from __future__ import annotations

import json
from typing import Any
from datetime import date

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType, NumberSelector, NumberSelectorConfig

from .const import (
    CONF_ALEXA_ACTIONS,
    CONF_ALEXA_ACTION_SCRIPT,
    CONF_ALEXA_SUPPRESS_CONFIRMATION,
    CONF_IMPEDANCE_WAIT_SECONDS,
    CONF_MAC,
    CONF_NOTIFY_ASSIGNED,
    CONF_NOTIFY_SERVICE,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_USER_NOTIFY_SERVICE,
    CONF_USER_ALEXA_DEVICE,
    CONF_USERS,
    DEFAULT_USERS_JSON,
    DOMAIN,
)


def _parse_users(value: str) -> list[dict[str, Any]]:
    users = json.loads(value)
    if not isinstance(users, list):
        raise ValueError("users must be a list")
    for user in users:
        if not isinstance(user, dict):
            raise ValueError("each user must be an object")
        for key in ("NAME",):
            if key not in user:
                raise ValueError(f"missing {key}")
        if "SEX" in user and user["SEX"] not in ("male", "female"):
            raise ValueError("SEX must be male or female")
        if CONF_USER_NOTIFY_SERVICE in user and not isinstance(user[CONF_USER_NOTIFY_SERVICE], str):
            raise ValueError(f"{CONF_USER_NOTIFY_SERVICE} must be a string")
        if CONF_USER_ALEXA_DEVICE in user and not isinstance(user[CONF_USER_ALEXA_DEVICE], str):
            raise ValueError(f"{CONF_USER_ALEXA_DEVICE} must be a string")
    names = [u["NAME"] for u in users]
    if any(not isinstance(n, str) or not n.strip() for n in names) or len(set(names)) != len(names):
        raise ValueError("unique non-empty names required")
    for user in users:
        if "GT" in user or "LT" in user:
            if not isinstance(user.get("GT"), (int, float)) or not isinstance(user.get("LT"), (int, float)) or not 0 <= user["GT"] < user["LT"] <= 250:
                raise ValueError("weight range must satisfy 0 <= GT < LT <= 250 kg")
        if "HEIGHT" in user and not 30 <= float(user["HEIGHT"]) <= 250:
            raise ValueError("invalid height in cm")
        if user.get("DOB"):
            from datetime import date
            if date.fromisoformat(user["DOB"]) >= date.today():
                raise ValueError("invalid date of birth")
        if user.get("NOTIFY_SERVICE") and not user["NOTIFY_SERVICE"].startswith("notify.mobile_app_"):
            raise ValueError("use a Home Assistant mobile_app notification service")
    return users


def _users_json(users: list[dict[str, Any]]) -> str:
    return json.dumps(users, indent=2)


def _schema(users, options=None, setup=False):
    options = options or {}
    fields = {}
    if setup:
        fields[vol.Required(CONF_NAME, default="Xiaomi Scale")] = str
        fields[vol.Required(CONF_MAC)] = str
    fields.update({
        vol.Optional(CONF_PERSISTENT_NOTIFICATION, default=options.get(CONF_PERSISTENT_NOTIFICATION, True)): bool,
        vol.Optional(CONF_NOTIFY_ASSIGNED, default=options.get(CONF_NOTIFY_ASSIGNED, True)): bool,
        vol.Optional(CONF_IMPEDANCE_WAIT_SECONDS, default=options.get(CONF_IMPEDANCE_WAIT_SECONDS, 8)): NumberSelector(NumberSelectorConfig(min=0,max=60,step=1,unit_of_measurement="s",mode="box")),
        vol.Required(CONF_USERS, default=_users_json(users)): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT,multiline=True)),
    })
    return vol.Schema(fields)

class XiaomiScaleHaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                users = _parse_users(user_input[CONF_USERS])
                import re
                mac = user_input[CONF_MAC].strip().upper()
                if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                    raise ValueError("invalid MAC")
            except (ValueError, TypeError, KeyError):
                errors["base"] = "invalid_users"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_NAME],
                    data={CONF_NAME:user_input[CONF_NAME], CONF_MAC:mac, CONF_USERS:users},
                    options={k:v for k,v in user_input.items() if k not in (CONF_NAME,CONF_MAC,CONF_USERS)})
        return self.async_show_form(step_id="user", data_schema=_schema([],setup=True), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return XiaomiScaleHaOptionsFlow()

class XiaomiScaleHaOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["edit_profile", "settings"])

    def _users(self):
        return json.loads(json.dumps(self.config_entry.options.get(CONF_USERS,self.config_entry.data.get(CONF_USERS, []))))

    async def async_step_edit_profile(self, user_input=None):
        if user_input is not None:
            self._profile_name = user_input["person"]
            return await self.async_step_profile()
        names = [u["NAME"] for u in self._users()]
        return self.async_show_form(step_id="edit_profile", data_schema=vol.Schema({vol.Required("person"):vol.In(names)}))

    async def async_step_profile(self, user_input=None):
        from homeassistant.helpers.selector import DateSelector, NumberSelector, NumberSelectorConfig, SelectSelector, SelectSelectorConfig
        users = self._users()
        user = next(u for u in users if u["NAME"]==self._profile_name)
        errors = {}
        if user_input is not None:
            changed = {**user, **{k:v for k,v in user_input.items() if k not in ('manual_range', 'birth_day', 'birth_month', 'birth_year')}}
            if not user_input.get("manual_range"):
                changed.pop("GT",None); changed.pop("LT",None)
            try:
                birth_parts = [user_input.get(k) for k in ('birth_year', 'birth_month', 'birth_day')]
                if any(v is not None for v in birth_parts):
                    changed['DOB'] = date(*(int(v) for v in birth_parts)).isoformat()
                if user_input.get("manual_range") and ("GT" not in changed or "LT" not in changed):
                    raise ValueError("Both bounds are required")
                _parse_users(json.dumps([changed]))
            except (ValueError,TypeError):
                errors["base"] = "invalid_profile"
            else:
                users = [changed if u["NAME"]==self._profile_name else u for u in users]
                return self.async_create_entry(title="",data={**self.config_entry.options,CONF_USERS:users})
        fields = {vol.Required("manual_range",default="GT" in user and "LT" in user):bool}
        if user.get("DOB"):
            born = date.fromisoformat(user["DOB"])
            user.update(birth_year=born.year, birth_month=str(born.month), birth_day=born.day)
        selectors = {
            "GT":NumberSelector(NumberSelectorConfig(min=0,max=250,step=0.1,unit_of_measurement="kg",mode="box")),
            "LT":NumberSelector(NumberSelectorConfig(min=0,max=250,step=0.1,unit_of_measurement="kg",mode="box")),
            "HEIGHT":NumberSelector(NumberSelectorConfig(min=30,max=250,step=1,unit_of_measurement="cm",mode="box")),
            "birth_day":NumberSelector(NumberSelectorConfig(min=1,max=31,step=1,mode="box")),
            "birth_month":SelectSelector(SelectSelectorConfig(options=[{"value":str(i),"label":label} for i,label in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"],1)],mode="dropdown")),
            "birth_year":NumberSelector(NumberSelectorConfig(min=1900,max=date.today().year,step=1,mode="box")),
            "SEX":SelectSelector(SelectSelectorConfig(options=[{"value":"male","label":"Male"},{"value":"female","label":"Female"}],mode="dropdown")),
        }
        for key,selector in selectors.items():
            fields[vol.Optional(key,default=user[key]) if key in user else vol.Optional(key)] = selector
        return self.async_show_form(step_id="profile",data_schema=vol.Schema(fields),errors=errors,
            description_placeholders={"person":self._profile_name})

    async def async_step_settings(self, user_input=None):
        options = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(title="",data={**options,**user_input})
        fields = {
            vol.Required(CONF_PERSISTENT_NOTIFICATION,default=options.get(CONF_PERSISTENT_NOTIFICATION,True)):bool,
            vol.Required(CONF_NOTIFY_ASSIGNED,default=options.get(CONF_NOTIFY_ASSIGNED,True)):bool,
            vol.Required(CONF_IMPEDANCE_WAIT_SECONDS,default=options.get(CONF_IMPEDANCE_WAIT_SECONDS,8)):NumberSelector(NumberSelectorConfig(min=0,max=60,step=1,unit_of_measurement="s",mode="box")),
        }
        return self.async_show_form(step_id="settings",data_schema=vol.Schema(fields))
