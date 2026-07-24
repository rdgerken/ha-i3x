"""Write support: PUT /objects/value and PUT /objects/history.

Two tiers, both spec-conformant and default-safe:

- **Idempotent echo writes always succeed as no-ops** — writing an entity's
  current value back (or re-writing an existing history point at its own
  timestamp) changes nothing, so it is accepted regardless of the write
  toggle. This is exactly the non-destructive write the conformance suite
  performs.
- **Value-changing writes** require the `write_enabled` option AND the entity
  to match the write allowlist, and are dispatched to the matching Home
  Assistant service per domain. Locks are never writable. Novel history
  points are always refused per-item — Home Assistant's recorder is
  append-only and provides no API to inject past states.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .http_util import ProblemError, item_not_found, item_ok, item_problem
from .model import AddressSpace, I3xObject
from .values import iso_z, state_to_value

TRUTHY = {True, 1, 1.0, "on", "true", "True", "1"}
FALSY = {False, 0, 0.0, "off", "false", "False", "0"}


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    try:
        if value in TRUTHY:
            return True
        if value in FALSY:
            return False
    except TypeError:
        return None
    return None


def _as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _dispatch(hass: HomeAssistant, obj: I3xObject, value) -> str | None:
    """Dispatch a value-changing write to the matching HA service.

    Returns None on success, or a human-readable refusal reason.
    """
    entity_id = obj.entity_id
    domain = entity_id.split(".", 1)[0]
    data = {"entity_id": entity_id}

    if domain == "lock":
        return "locks are not writable via i3X"

    async def call(service_domain: str, service: str, service_data: dict) -> None:
        await hass.services.async_call(
            service_domain, service, service_data, blocking=True
        )

    # Structured domains accept their value object; leaf booleans a truthy.
    if domain in ("switch", "input_boolean", "siren", "remote", "fan", "humidifier",
                  "automation", "script", "light"):
        state_field = value.get("state") if isinstance(value, dict) else value
        on = _as_bool(state_field)
        if on is None:
            return f"cannot interpret {value!r} as on/off"
        if domain == "light" and on and isinstance(value, dict):
            for attr in ("brightness", "color_temp_kelvin"):
                num = _as_float(value.get(attr))
                if num is not None:
                    data[attr] = int(num)
        if domain == "fan" and on and isinstance(value, dict):
            num = _as_float(value.get("percentage"))
            if num is not None:
                data["percentage"] = int(num)
        await call(domain, "turn_on" if on else "turn_off", data)
        return None

    if domain in ("number", "input_number"):
        num = _as_float(value)
        if num is None:
            return f"cannot interpret {value!r} as a number"
        await call(domain, "set_value", {**data, "value": num})
        return None

    if domain in ("text", "input_text"):
        if not isinstance(value, str):
            return "value must be a string"
        await call(domain, "set_value", {**data, "value": value})
        return None

    if domain in ("select", "input_select"):
        if not isinstance(value, str):
            return "value must be a string option"
        await call(domain, "select_option", {**data, "option": value})
        return None

    if domain == "cover":
        if isinstance(value, dict):
            pos = _as_float(value.get("current_position"))
            if pos is not None:
                await call(domain, "set_cover_position", {**data, "position": int(pos)})
                return None
            value = value.get("state")
        num = _as_float(value)
        if num is not None:
            await call(domain, "set_cover_position", {**data, "position": int(num)})
            return None
        on = _as_bool(value if value not in ("open", "closed") else value == "open")
        if on is None:
            return f"cannot interpret {value!r} as a cover command"
        await call(domain, "open_cover" if on else "close_cover", data)
        return None

    if domain == "climate":
        if isinstance(value, dict):
            temp = _as_float(value.get("temperature"))
            if temp is not None:
                await call(domain, "set_temperature", {**data, "temperature": temp})
                return None
            value = value.get("state")
        if isinstance(value, str):
            await call(domain, "set_hvac_mode", {**data, "hvac_mode": value})
            return None
        return f"cannot interpret {value!r} as a climate command"

    if domain in ("button", "input_button"):
        await call(domain, "press", data)
        return None
    if domain == "scene":
        await call(domain, "turn_on", data)
        return None

    return f"writes are not supported for the {domain} domain"


def _current_value(hass: HomeAssistant, obj: I3xObject):
    """(value, quality, state) for an entity-backed object, else Nones."""
    if obj.entity_id is None or obj.typing is None:
        return None, None, None
    state = hass.states.get(obj.entity_id)
    if state is None:
        return None, None, None
    value, quality = state_to_value(state, obj.typing)
    return value, quality, state


async def write_values(
    hass: HomeAssistant,
    snapshot: AddressSpace,
    updates: list,
    write_enabled: bool,
    write_filter,
) -> list[dict]:
    """Handle PUT /objects/value as a bulk of per-item results."""
    results: list[dict] = []
    for update in updates:
        if not isinstance(update, dict) or not isinstance(
            update.get("elementId"), str
        ):
            raise ProblemError(400, "Bad Request", "updates[].elementId is required")
        element_id = update["elementId"]
        vqt = update.get("value")
        if not isinstance(vqt, dict) or "value" not in vqt:
            results.append(
                item_problem(
                    "elementId",
                    element_id,
                    400,
                    "Bad Request",
                    "updates[].value.value is required",
                )
            )
            continue
        obj = snapshot.objects.get(element_id)
        if obj is None:
            results.append(item_not_found("elementId", element_id))
            continue
        written = vqt["value"]
        current, quality, state = _current_value(hass, obj)
        if state is None:
            results.append(
                item_problem(
                    "elementId",
                    element_id,
                    400,
                    "Bad Request",
                    "This object has no live value and is not writable",
                )
            )
            continue
        if written == current and quality == "Good":
            # Idempotent echo write: no-op success.
            results.append(item_ok("elementId", element_id, None))
            continue
        if not write_enabled:
            results.append(
                item_problem(
                    "elementId",
                    element_id,
                    403,
                    "Forbidden",
                    "Value-changing writes are disabled; enable them in the "
                    "i3X options",
                )
            )
            continue
        if not write_filter(obj.entity_id):
            results.append(
                item_problem(
                    "elementId",
                    element_id,
                    403,
                    "Forbidden",
                    "This entity is not in the write allowlist; add it in the "
                    "i3X options (an empty allowlist permits all entities)",
                )
            )
            continue
        try:
            refusal = await _dispatch(hass, obj, written)
        except HomeAssistantError as err:
            results.append(
                item_problem(
                    "elementId", element_id, 500, "Internal Server Error", str(err)
                )
            )
            continue
        if refusal is not None:
            results.append(
                item_problem("elementId", element_id, 400, "Bad Request", refusal)
            )
        else:
            results.append(item_ok("elementId", element_id, None))
    return results


async def write_history(
    hass: HomeAssistant, snapshot: AddressSpace, updates: list
) -> list[dict]:
    """Handle PUT /objects/history: idempotent replacement only.

    A write is accepted when the (timestamp, value) pair already exists —
    matched against the live state or the recorder — i.e. a record replaced
    with itself. Novel points are refused per-item: the recorder is
    append-only.
    """
    from homeassistant.components.recorder import get_instance, history

    results: list[dict] = []
    for update in updates:
        if not isinstance(update, dict) or not isinstance(
            update.get("elementId"), str
        ):
            raise ProblemError(400, "Bad Request", "updates[].elementId is required")
        element_id = update["elementId"]
        vqt = update.get("value")
        if (
            not isinstance(vqt, dict)
            or "value" not in vqt
            or not isinstance(vqt.get("timestamp"), str)
        ):
            results.append(
                item_problem(
                    "elementId",
                    element_id,
                    400,
                    "Bad Request",
                    "value and timestamp are required for historical writes",
                )
            )
            continue
        obj = snapshot.objects.get(element_id)
        if obj is None:
            results.append(item_not_found("elementId", element_id))
            continue
        written_ts = dt_util.parse_datetime(vqt["timestamp"])
        if written_ts is None:
            results.append(
                item_problem(
                    "elementId",
                    element_id,
                    400,
                    "Bad Request",
                    "timestamp must be a valid RFC 3339 timestamp",
                )
            )
            continue
        written = vqt["value"]

        # Cheap path: matches the entity's current point.
        current, quality, state = _current_value(hass, obj)
        if (
            state is not None
            and written == current
            and iso_z(state.last_updated) == iso_z(written_ts)
        ):
            results.append(item_ok("elementId", element_id, None))
            continue

        # Recorder lookup: does this exact point already exist?
        matched = False
        if obj.entity_id is not None and obj.typing is not None:
            window = timedelta(seconds=1)
            states_map = await get_instance(hass).async_add_executor_job(
                lambda eid=obj.entity_id, ts=written_ts: history.get_significant_states(
                    hass,
                    ts - window,
                    ts + window,
                    [eid],
                    include_start_time_state=True,
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=False,
                )
            )
            for past in states_map.get(obj.entity_id, ()):
                past_value, _ = state_to_value(past, obj.typing)
                if past_value == written and iso_z(past.last_updated) == iso_z(
                    written_ts
                ):
                    matched = True
                    break
        if matched:
            results.append(item_ok("elementId", element_id, None))
        else:
            results.append(
                item_problem(
                    "elementId",
                    element_id,
                    400,
                    "Bad Request",
                    "Only idempotent replacements are supported: Home Assistant's "
                    "recorder is append-only, so new or altered history points "
                    "cannot be written",
                )
            )
    return results
