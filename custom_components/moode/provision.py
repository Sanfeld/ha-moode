"""Set a moOde box up for multiroom: as a receiver in an existing zone, or as a sender.

Everything goes through moOde's own Configure -> Multiroom page, one setting at a time,
exactly as clicking it in moOde would. Nothing else on the box is touched (audio device,
DSP, volume type...)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import MoodeClient, MoodeError, TrxForm
from .const import CONF_RECEIVERS, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Receiver settings copied from a receiver that already works in the zone. Max volume
# (ALSA) is left out on purpose: it depends on the box's own sound card.
RX_COPY = {
    "multiroom_rx_mastervol_opt_in": "Master volume opt-in",
    "multiroom_rx_alsa_output_mode": "ALSA output mode",
    "multiroom_rx_bfr": "Receiver buffer",
    "multiroom_rx_jitter_bfr": "Jitter buffer",
    "multiroom_rx_frame_size": "Frame size",
    "multiroom_rx_rtprio": "Realtime priority",
}
# These start a job in moOde's worker; give it time before the next one.
JOB_FIELDS = {"multiroom_rx", "multiroom_tx", "alsa_loopback"}
JOB_PAUSE = 6


@dataclass
class Step:
    label: str
    field: str
    value: str

    @property
    def button(self) -> str:
        return f"update_{self.field}"


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def set(self, form: TrxForm, name: str, value: str | None, label: str) -> None:
        """Add a step if the box has this setting, accepts the value and differs."""
        if value is None or name not in form.values and name not in form.choices:
            return
        if name in form.choices and form.choices[name] and value not in form.choices[name]:
            self.notes.append(f"{label}: left as is ({value} isn't available on this box)")
            return
        if form.values.get(name) != value:
            self.steps.append(Step(f"{label}: {form.values.get(name) or '-'} → {value}", name, value))


def _check_usable(form: TrxForm, what: str, field_name: str) -> None:
    if field_name in form.disabled:
        raise MoodeError(f"moOde won't allow the multiroom {what} on this box while DSP "
                         "(CamillaDSP, equalizer, crossfeed or polarity inversion) is on. "
                         "Turn DSP off in moOde first.")


async def plan_receiver(target: MoodeClient, sender: MoodeClient,
                        template: MoodeClient | None = None) -> Plan:
    if target.host == sender.host:
        raise MoodeError("A box can't be a receiver of itself")
    tgt, _ = await target.trx_read()
    src, _ = await sender.trx_read()
    _check_usable(tgt, "receiver", "multiroom_rx")
    plan = Plan()
    if src.values.get("multiroom_tx") != "On":
        plan.notes.append("The sender's multiroom sender is off: switch it on there (or set "
                          "that box up as a sender) or the new room won't hear anything")
    plan.set(tgt, "multiroom_rx_host", src.values.get("multiroom_tx_host"), "Multicast address")
    if template is not None:
        tpl, _ = await template.trx_read()
        for name, label in RX_COPY.items():
            plan.set(tgt, name, tpl.values.get(name), label)
    if tgt.values.get("multiroom_rx") == "Disabled":
        plan.steps.append(Step("Receiver: Disabled → On", "multiroom_rx", "On"))
    return plan


async def plan_sender(target: MoodeClient) -> Plan:
    tgt, _ = await target.trx_read()
    plan = Plan()
    if tgt.values.get("alsa_loopback") != "On":
        plan.steps.append(Step("Loopback: Off → On", "alsa_loopback", "On"))
    if tgt.values.get("multiroom_tx") != "On":
        if "multiroom_tx" in tgt.disabled and tgt.values.get("alsa_loopback") == "On":
            _check_usable(tgt, "sender", "multiroom_tx")
        plan.steps.append(Step("Sender: Off → On", "multiroom_tx", "On"))
    return plan


async def apply(client: MoodeClient, plan: Plan) -> list[str]:
    done = []
    for i, step in enumerate(plan.steps):
        if i and plan.steps[i - 1].field in JOB_FIELDS:
            await asyncio.sleep(JOB_PAUSE)
        after = await client.trx_set(step.button, {step.field: step.value})
        if after.values and after.values.get(step.field) not in (step.value, None):
            raise MoodeError(f"moOde didn't accept {step.label} (it shows "
                             f"{after.values.get(step.field)}); done so far: {', '.join(done) or 'nothing'}")
        done.append(step.label)
    if plan.steps and plan.steps[-1].field in JOB_FIELDS:
        await asyncio.sleep(JOB_PAUSE)
    return done


async def discover_receivers(sender: MoodeClient) -> None:
    """Have the sender look for its receivers again, like moOde's Discover button, so it
    knows the new room (initial volume and master volume follow it)."""
    try:
        await sender.trx_set("multiroom_tx_discover")
    except MoodeError as err:  # not fatal: the zone works, only moOde's own list is stale
        _LOGGER.warning("%s: Discover receivers failed: %s", sender.host, err)


def join_zone(hass: HomeAssistant, sender_entry: ConfigEntry, receiver_entry_id: str) -> None:
    rooms = list(sender_entry.options.get(CONF_RECEIVERS, []))
    if receiver_entry_id not in rooms:
        hass.config_entries.async_update_entry(
            sender_entry, options={**sender_entry.options, CONF_RECEIVERS: rooms + [receiver_entry_id]})


async def join_zone_when_added(hass: HomeAssistant, unique_id: str, sender_entry_id: str) -> None:
    """For a box set up from the add-device dialog: its config entry only exists once the
    dialog is finished, so wait for it and then add it to the zone."""
    for _ in range(120):
        entry = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
        sender = hass.config_entries.async_get_entry(sender_entry_id)
        if entry is not None and sender is not None:
            join_zone(hass, sender, entry.entry_id)
            return
        await asyncio.sleep(1)
    _LOGGER.warning("Couldn't add %s to the zone: the box didn't appear in time", unique_id)
