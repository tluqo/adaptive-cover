"""The Coordinator for Adaptive Cover."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

import numpy as np
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    DOMAIN as COVER_DOMAIN,
)

# from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State
from homeassistant.helpers.template import state_attr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .calculation import (
    AdaptiveHorizontalCover,
    AdaptiveTiltCover,
    AdaptiveVerticalCover,
    ClimateCoverData,
    ClimateCoverState,
    NormalCoverState,
)

from .const import (
    _LOGGER,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CONF_AWNING_ANGLE,
    CONF_AZIMUTH,
    CONF_BLIND_SPOT_ELEVATION,
    CONF_BLIND_SPOT_LEFT,
    CONF_BLIND_SPOT_RIGHT,
    CONF_CLIMATE_MODE,
    CONF_DEFAULT_POSITION,
    CONF_DEFAULT_HEIGHT,
    CONF_DELTA_POSITION,
    CONF_DELTA_TIME,
    CONF_DISTANCE,
    CONF_ENABLE_BLIND_SPOT,
    CONF_END_ENTITY,
    CONF_END_TIME,
    CONF_ENTITIES,
    CONF_FOV_LEFT,
    CONF_FOV_RIGHT,
    CONF_HEIGHT_WIN,
    CONF_INTERP,
    CONF_INTERP_END,
    CONF_INTERP_LIST,
    CONF_INTERP_LIST_NEW,
    CONF_INTERP_START,
    CONF_INVERSE_STATE,
    CONF_IRRADIANCE_ENTITY,
    CONF_IRRADIANCE_THRESHOLD,
    CONF_LENGTH_AWNING,
    CONF_LUX_ENTITY,
    CONF_LUX_THRESHOLD,
    CONF_MANUAL_IGNORE_INTERMEDIATE,
    CONF_MANUAL_OVERRIDE_DURATION,
    CONF_MANUAL_OVERRIDE_RESET,
    CONF_MANUAL_THRESHOLD,
    CONF_MAX_ELEVATION,
    CONF_MAX_POSITION,
    CONF_MIN_ELEVATION,
    CONF_OUTSIDETEMP_ENTITY,
    CONF_PRESENCE_ENTITY,
    CONF_START_ENTITY,
    CONF_START_TIME,
    CONF_SUNRISE_OFFSET,
    CONF_SUNRISE_OPEN_SPEED,
    CONF_SUNSET_OFFSET,
    CONF_SUNSET_POS,
    CONF_SUNSET_TILT,
    CONF_TEMP_ENTITY,
    CONF_TEMP_HIGH,
    CONF_TEMP_LOW,
    CONF_TILT_DEPTH,
    CONF_TILT_DISTANCE,
    CONF_TILT_MODE,
    CONF_TRANSPARENT_BLIND,
    CONF_WEATHER_ENTITY,
    CONF_WEATHER_STATE,
    DOMAIN,
    LOGGER,
)
from .helpers import get_datetime_from_str, get_last_updated, get_safe_state


@dataclass
class StateChangedData:
    """StateChangedData class."""

    entity_id: str
    old_state: State | None
    new_state: State | None


@dataclass
class AdaptiveCoverData:
    """AdaptiveCoverData class."""

    climate_mode_toggle: bool
    states: dict
    attributes: dict


class AdaptiveDataUpdateCoordinator(DataUpdateCoordinator[AdaptiveCoverData]):
    """Adaptive cover data update coordinator."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant) -> None:  # noqa: D107
        super().__init__(hass, LOGGER, name=DOMAIN)

        self._cover_type = self.config_entry.data.get("sensor_type")
        self._climate_mode = self.config_entry.options.get(CONF_CLIMATE_MODE, False)
        self._switch_mode = True if self._climate_mode else False
        self._inverse_state = self.config_entry.options.get(CONF_INVERSE_STATE, False)
        self._use_interpolation = self.config_entry.options.get(CONF_INTERP, False)
        self._temp_toggle = None
        self._control_toggle = None
        self._manual_toggle = None
        self._lux_toggle = None
        self._irradiance_toggle = None
        self.manual_reset = self.config_entry.options.get(
            CONF_MANUAL_OVERRIDE_RESET, False
        )
        self.manual_duration = self.config_entry.options.get(
            CONF_MANUAL_OVERRIDE_DURATION, {"minutes": 15}
        )
        self.state_change = False
        self.cover_state_change = False
        self.first_refresh = False
        self.timed_refresh = False
        self.climate_state = None
        self.climate_state_pos = None
        self.control_method = "intermediate"
        self.state_change_data: StateChangedData | None = None
        self.manager = AdaptiveCoverManager(self.manual_duration)
        self.wait_for_target = {}
        self.target_call = {}
        self.target_attr = {}
        self.subsequent_target_call = {}
        self.subsequent_target_attr = {}
        self.ignore_intermediate_states = self.config_entry.options.get(
            CONF_MANUAL_IGNORE_INTERMEDIATE, False
        )

    async def async_config_entry_first_refresh(self) -> None:
        """Config entry first refresh."""
        self.first_refresh = True
        await super().async_config_entry_first_refresh()
        _LOGGER.debug("Config entry first refresh")

    async def async_timed_refresh(self, event) -> None:
        """Control state at end time."""

        if self.end_time is not None:
            time = self.end_time
        if self.end_time_entity is not None:
            time = get_safe_state(self.hass, self.end_time_entity)
        time_check = dt.datetime.now() - get_datetime_from_str(time)
        if time is not None and (time_check <= dt.timedelta(seconds=1)):
            self.timed_refresh = True
            _LOGGER.debug("Timed refresh triggered")
            await self.async_refresh()
        else:
            _LOGGER.debug("Time not equal to end time")

    async def async_check_entity_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Fetch and process state change event."""
        _LOGGER.debug("Entity state change")
        self.state_change = True
        await self.async_refresh()

    async def async_check_cover_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Fetch and process state change event."""
        _LOGGER.debug("Cover state change")
        data = event.data
        if data["old_state"] is None:
            _LOGGER.debug("Old state is None")
            return
        self.state_change_data = StateChangedData(
            data["entity_id"], data["old_state"], data["new_state"]
        )
        self.cover_state_change = True
        await self.process_entity_state_change()
        await self.async_refresh()

    async def process_entity_state_change(self):
        """Process state change event."""
        event = self.state_change_data
        _LOGGER.debug("Processing state change event: %s", event)
        entity_id = event.entity_id
        if self.ignore_intermediate_states and event.new_state.state in [
            "opening",
            "closing",
        ]:
            _LOGGER.debug("Ignoring intermediate state change for %s", entity_id)
            return
        if self.wait_for_target.get(entity_id):
            attribute = ATTR_CURRENT_TILT_POSITION
            if (
                self.target_attr.get(entity_id)
                and self.target_attr.get(entity_id) == "position"
            ) or self._cover_type != "cover_tilt":
                attribute = "current_position"
            position = event.new_state.attributes.get(attribute)
            if position == self.target_call.get(entity_id):
                _LOGGER.debug("Position %s reached for %s", position, entity_id)
                if self.subsequent_target_attr.get(entity_id):
                    tilt = self.subsequent_target_call.get(entity_id)
                    self.subsequent_target_call[entity_id] = None
                    self.subsequent_target_attr[entity_id] = None
                    await self.async_set_position_and_tilt(entity_id, position, tilt)
                else:
                    self.wait_for_target[entity_id] = False

        _LOGGER.debug("Wait for target: %s", self.wait_for_target)

    async def _async_update_data(self) -> AdaptiveCoverData:
        options = self.config_entry.options
        self._update_options(options)

        # Get data for the blind
        cover_data = self.get_blind_data(options=options)

        # Update manager with covers
        self._update_manager_and_covers()

        # Access climate data if climate mode is enabled
        if self._climate_mode:
            self.climate_mode_data(options, cover_data)

        # calculate the state of the cover
        self.normal_cover_state = NormalCoverState(cover_data)

        self.default_state = round(self.normal_cover_state.get_state())
        state = self.state

        self.default_state_pos = round(self.normal_cover_state.get_state_pos())
        state_pos = self.state_pos

        await self.manager.reset_if_needed()

        # Handle types of changes
        if self.state_change and not self._cover_type == "cover_tilt":
            await self.async_handle_state_change(state, options)
        if self.state_change and self._cover_type == "cover_tilt":
            await self.async_handle_state_change2(state_pos, state, options)
        if self.cover_state_change and not self._cover_type == "cover_tilt":
            await self.async_handle_cover_state_change(state)
        if self.cover_state_change and self._cover_type == "cover_tilt":
            await self.async_handle_cover_state_change2(state_pos, state)
        if self.first_refresh:
            await self.async_handle_first_refresh(state, options)
        if self.timed_refresh:
            await self.async_handle_timed_refresh(options)

        normal_cover = self.normal_cover_state.cover
        # Run the solar_times method in a separate thread
        loop = asyncio.get_event_loop()
        start, end = await loop.run_in_executor(None, normal_cover.solar_times)
        return AdaptiveCoverData(
            climate_mode_toggle=self.switch_mode,
            states={
                "state": state,
                "start": start,
                "end": end,
                "control": self.control_method,
                "sun_motion": normal_cover.valid,
                "manual_override": self.manager.binary_cover_manual,
                "manual_list": self.manager.manual_controlled,
            },
            attributes={
                "default": options.get(CONF_DEFAULT_HEIGHT),
                "default_pos": options.get(CONF_DEFAULT_POSITION),
                "sunset_default": options.get(CONF_SUNSET_POS),
                "sunset_default_tilt": options.get(CONF_SUNSET_TILT),
                "sunset_offset": options.get(CONF_SUNSET_OFFSET),
                "azimuth_window": options.get(CONF_AZIMUTH),
                "field_of_view": [
                    options.get(CONF_FOV_LEFT),
                    options.get(CONF_FOV_RIGHT),
                ],
                "blind_spot": options.get(CONF_BLIND_SPOT_ELEVATION),
            },
        )

    async def async_handle_state_change(self, state: int, options):
        """Handle state change from tracked entities."""
        if self.control_toggle:
            for cover in self.entities:
                await self.async_handle_call_service(cover, state, options)
        else:
            _LOGGER.debug("State change but control toggle is off")
        self.state_change = False

    async def async_handle_state_change2(self, position: int, tilt: int, options):
        """Handle state change from tracked entities."""
        if self.control_toggle:
            for cover in self.entities:
                await self.async_handle_call_service2(cover, position, tilt, options)
        else:
            _LOGGER.debug("State change but control toggle is off")
        self.state_change = False

    async def async_handle_cover_state_change(self, state: int):
        """Handle state change from assigned covers."""
        if self.manual_toggle and self.control_toggle:
            self.manager.handle_state_change(
                self.state_change_data,
                state,
                self._cover_type,
                self.manual_reset,
                self.wait_for_target,
                self.manual_threshold,
            )
        self.cover_state_change = False

    async def async_handle_cover_state_change2(self, position: int, tilt: int):
        """Handle state change from assigned covers."""
        if self.manual_toggle and self.control_toggle:
            self.manager.handle_state_change2(
                self.state_change_data,
                position,
                tilt,
                self._cover_type,
                self.manual_reset,
                self.wait_for_target,
                self.manual_threshold,
            )
        self.cover_state_change = False

    # todo: handle pos and tilt for venetian blinds
    async def async_handle_first_refresh(self, state: int, options):
        """Handle first refresh."""
        if self.control_toggle:
            for cover in self.entities:
                if (
                    self.check_adaptive_time
                    and not self.manager.is_cover_manual(cover)
                    and self.check_position(cover, state, options)
                ):
                    await self.async_set_position(cover, state)
        else:
            _LOGGER.debug("First refresh but control toggle is off")
        self.first_refresh = False

    async def async_handle_timed_refresh(self, options):
        """Handle timed refresh."""
        if self.control_toggle:
            for cover in self.entities:
                await self.async_set_manual_position(
                    cover,
                    (
                        inverse_state(options.get(CONF_SUNSET_POS))
                        if self._inverse_state
                        else options.get(CONF_SUNSET_POS)
                    ),
                )
        else:
            _LOGGER.debug("Timed refresh but control toggle is off")
        self.timed_refresh = False

    async def async_handle_call_service(self, entity, state: int, options):
        """Handle call service."""
        if (
            self.check_position(entity, state, options)
            and self.check_time_delta(entity)
            and self.check_adaptive_time
            and not self.manager.is_cover_manual(entity)
        ):
            await self.async_set_position(entity, state)

    async def async_handle_call_service2(
        self, entity, position: int, tilt: int, options
    ):
        """Handle call service."""
        if (
            self.check_position2(entity, position, tilt, options)
            and self.check_time_delta(entity)
            and self.check_adaptive_time
            and not self.manager.is_cover_manual(entity)
        ):
            await self.async_set_position_and_tilt(entity, position, tilt)

    async def async_set_position(self, entity, state: int):
        """Call service to set cover position."""
        await self.async_set_manual_position(entity, state)

    async def async_set_position_and_tilt(self, entity, position: int, tilt: int):
        """Call service to set cover position."""
        await self.async_set_manual_position_and_tilt(entity, position, tilt)

    async def async_set_manual_position(self, entity, position):
        """Call service to set cover position."""
        service = SERVICE_SET_COVER_POSITION
        service_data = {}
        service_data[ATTR_ENTITY_ID] = entity

        if self._cover_type == "cover_tilt":
            service = SERVICE_SET_COVER_TILT_POSITION
            service_data[ATTR_TILT_POSITION] = position
            self.target_attr[entity] = "tilt"
        else:
            service_data[ATTR_POSITION] = position
            self.target_attr[entity] = "position"

        self.wait_for_target[entity] = True
        self.target_call[entity] = position

        _LOGGER.debug(
            "Set wait for target %s and target call %s",
            self.wait_for_target,
            self.target_call,
        )
        _LOGGER.debug("Run %s with data %s", service, service_data)
        await self.hass.services.async_call(COVER_DOMAIN, service, service_data)

    async def async_set_manual_position_and_tilt(self, entity, position, tilt):
        """Call service to set cover position."""

        if self._cover_type == "cover_tilt":
            cur_position = state_attr(self.hass, entity, "current_position")

            if position != cur_position:
                service = SERVICE_SET_COVER_POSITION
                service_data = {}
                service_data[ATTR_ENTITY_ID] = entity

                service_data[ATTR_POSITION] = position

                self.wait_for_target[entity] = True
                self.target_call[entity] = position
                self.target_attr[entity] = "position"
                self.subsequent_target_call[entity] = tilt
                self.subsequent_target_attr[entity] = "tilt"
                _LOGGER.debug(
                    "Set wait for target %s and target call %s",
                    self.wait_for_target,
                    self.target_call,
                )
                _LOGGER.debug("Run %s with data %s", service, service_data)
                await self.hass.services.async_call(COVER_DOMAIN, service, service_data)
            else:
                service = SERVICE_SET_COVER_TILT_POSITION
                service_data = {}
                service_data[ATTR_ENTITY_ID] = entity

                service_data[ATTR_TILT_POSITION] = tilt

                self.wait_for_target[entity] = True
                self.target_call[entity] = tilt
                self.target_attr[entity] = "tilt"
                _LOGGER.debug(
                    "Set wait for target %s and target call %s",
                    self.wait_for_target,
                    self.target_call,
                )

                _LOGGER.debug("Run %s with data %s", service, service_data)
                await self.hass.services.async_call(COVER_DOMAIN, service, service_data)
        else:
            service = SERVICE_SET_COVER_POSITION
            service_data = {}
            service_data[ATTR_ENTITY_ID] = entity

            service_data[ATTR_POSITION] = position

            self.wait_for_target[entity] = True
            self.target_call[entity] = position
            self.target_attr[entity] = "position"
            _LOGGER.debug(
                "Set wait for target %s and target call %s",
                self.wait_for_target,
                self.target_call,
            )
            _LOGGER.debug("Run %s with data %s", service, service_data)
            await self.hass.services.async_call(COVER_DOMAIN, service, service_data)

    # SERVICE_SET_COVER_POSITION: Final = "set_cover_position"
    # SERVICE_SET_COVER_TILT_POSITION: Final = "set_cover_tilt_position"

    def _update_options(self, options):
        """Update options."""
        self.entities = options.get(CONF_ENTITIES, [])
        self.min_change = options.get(CONF_DELTA_POSITION, 1)
        self.time_threshold = options.get(CONF_DELTA_TIME, 2)
        self.start_time = options.get(CONF_START_TIME)
        self.start_time_entity = options.get(CONF_START_ENTITY)
        self.end_time = options.get(CONF_END_TIME)
        self.end_time_entity = options.get(CONF_END_ENTITY)
        self.manual_reset = options.get(CONF_MANUAL_OVERRIDE_RESET, False)
        self.manual_duration = options.get(
            CONF_MANUAL_OVERRIDE_DURATION, {"minutes": 15}
        )
        self.manual_threshold = options.get(CONF_MANUAL_THRESHOLD)
        self.start_value = options.get(CONF_INTERP_START)
        self.end_value = options.get(CONF_INTERP_END)
        self.normal_list = options.get(CONF_INTERP_LIST)
        self.new_list = options.get(CONF_INTERP_LIST_NEW)

    def _update_manager_and_covers(self):
        self.manager.add_covers(self.entities)
        if not self._manual_toggle:
            for entity in self.manager.manual_controlled:
                self.manager.reset(entity)

    def get_blind_data(self, options):
        """Assign correct class for type of blind."""
        if self._cover_type == "cover_blind":
            cover_data = AdaptiveVerticalCover(
                self.hass,
                *self.pos_sun,
                *self.common_data(options),
                *self.vertical_data(options),
            )
        if self._cover_type == "cover_awning":
            cover_data = AdaptiveHorizontalCover(
                self.hass,
                *self.pos_sun,
                *self.common_data(options),
                *self.vertical_data(options),
                *self.horizontal_data(options),
            )
        if self._cover_type == "cover_tilt":
            cover_data = AdaptiveTiltCover(
                self.hass,
                *self.pos_sun,
                *self.common_data(options),
                *self.tilt_data(options),
            )
        return cover_data

    @property
    def check_adaptive_time(self):
        """Check if time is within start and end times."""
        return self.before_end_time and self.after_start_time

    @property
    def after_start_time(self):
        """Check if time is after start time."""
        now = dt.datetime.now()
        if self.start_time_entity is not None:
            time = get_datetime_from_str(
                get_safe_state(self.hass, self.start_time_entity)
            )
            _LOGGER.debug(
                "Start time: %s, now: %s, now >= time: %s ", time, now, now >= time
            )
            return now >= time
        if self.start_time is not None:
            time = get_datetime_from_str(self.start_time)

            _LOGGER.debug(
                "Start time: %s, now: %s, now >= time: %s", time, now, now >= time
            )
            return now >= time
        return True

    @property
    def how_much_after_start_time(self):
        """Check if time is after start time."""

        if self.start_time is not None:
            time = self.start_time
        if self.start_time_entity is not None:
            time_entity = get_safe_state(self.hass, self.start_time_entity)
            if time_entity is not None:
                time = time_entity
        return dt.datetime.now() - get_datetime_from_str(time)

    @property
    def is_wakeup_time(self):
        """Checks if time is after start time."""
        return False

        time_check = self.how_much_after_start_time
        return time_check < dt.timedelta(hours=1)

    @property
    def before_end_time(self):
        """Check if time is before end time."""
        now = dt.datetime.now()
        if self.end_time_entity is not None:
            time = get_datetime_from_str(
                get_safe_state(self.hass, self.end_time_entity)
            )
            _LOGGER.debug(
                "End time: %s, now: %s, now < time: %s", time, now, now < time
            )
            return now < time
        if self.end_time is not None:
            time = get_datetime_from_str(self.end_time)
            if time.time() == dt.time(0, 0):
                time = time + dt.timedelta(days=1)
            now = dt.datetime.now()
            _LOGGER.debug(
                "End time: %s, now: %s, now < time: %s", time, now, now < time
            )
            return now < time
        return True

    def check_position(self, entity, state: int, options):
        """Check cover positions to reduce calls."""
        if self._cover_type == "cover_tilt":
            position = state_attr(self.hass, entity, ATTR_CURRENT_TILT_POSITION)
        else:
            position = state_attr(self.hass, entity, ATTR_CURRENT_POSITION)
        if position is not None:
            condition = abs(position - state) >= self.min_change
            _LOGGER.debug(
                "Entity: %s,  position: %s, state: %s, delta position: %s, min_change: %s, condition: %s",
                entity,
                position,
                state,
                abs(position - state),
                self.min_change,
                condition,
            )
            if state in [
                options.get(CONF_SUNSET_POS),
                options.get(CONF_DEFAULT_HEIGHT),
                0,
                100,
            ]:
                condition = True
            return condition
        return True

    def check_position2(self, entity, state_pos: int, state_tilt: int, options):
        """Check cover positions to reduce calls."""
        if self._cover_type == "cover_tilt":
            position = state_attr(self.hass, entity, ATTR_CURRENT_POSITION)
            tilt = state_attr(self.hass, entity, ATTR_CURRENT_TILT_POSITION)
        else:
            position = state_attr(self.hass, entity, ATTR_CURRENT_POSITION)
            tilt = 0
        if position is not None and tilt is not None:
            # todo: weird hack
            is_to_edge_position = False
            positions = [
                options.get(CONF_SUNSET_POS),
                options.get(CONF_SUNSET_TILT),
                options.get(CONF_DEFAULT_HEIGHT),
                options.get(CONF_DEFAULT_POSITION),
                0,
                100,
            ]
            if state_tilt in positions:
                is_to_edge_position = True

            condition = (
                abs(position - state_pos) >= self.min_change
                or abs(tilt - state_tilt) >= self.min_change
                or abs(tilt - state_tilt) > 0
                and is_to_edge_position
            )

            _LOGGER.debug(
                "Entity: %s,  position: %s, state_pos: %s, tilt: %s, state_tilt: %s, delta position: %s, delta tilt: %s, min_change: %s, condition: %s",
                entity,
                position,
                state_pos,
                tilt,
                state_tilt,
                abs(position - state_pos),
                abs(tilt - state_tilt),
                self.min_change,
                condition,
            )

            return condition
        return True

    def check_time_delta(self, entity):
        """Check if time delta is passed."""
        now = dt.datetime.now(dt.UTC)
        last_updated = get_last_updated(entity, self.hass)
        if last_updated is not None:
            condition = now - last_updated >= dt.timedelta(minutes=self.time_threshold)
            _LOGGER.debug(
                "Entity: %s, time delta: %s, threshold: %s, condition: %s",
                entity,
                now - last_updated,
                self.time_threshold,
                condition,
            )
            return condition
        return True

    @property
    def pos_sun(self):
        """Fetch information for sun position."""
        return [
            state_attr(self.hass, "sun.sun", "azimuth"),
            state_attr(self.hass, "sun.sun", "elevation"),
        ]

    def common_data(self, options):
        """Update shared parameters."""
        return [
            options.get(CONF_SUNSET_POS),
            options.get(CONF_SUNSET_TILT),
            options.get(CONF_SUNSET_OFFSET),
            options.get(CONF_SUNRISE_OFFSET, options.get(CONF_SUNSET_OFFSET)),
            options.get(CONF_SUNRISE_OPEN_SPEED),
            self.hass.config.time_zone,
            options.get(CONF_FOV_LEFT),
            options.get(CONF_FOV_RIGHT),
            options.get(CONF_AZIMUTH),
            options.get(CONF_DEFAULT_HEIGHT),
            options.get(CONF_DEFAULT_POSITION),
            options.get(CONF_MAX_POSITION, 100),
            options.get(CONF_BLIND_SPOT_LEFT),
            options.get(CONF_BLIND_SPOT_RIGHT),
            options.get(CONF_BLIND_SPOT_ELEVATION),
            options.get(CONF_ENABLE_BLIND_SPOT, False),
            options.get(CONF_MIN_ELEVATION, None),
            options.get(CONF_MAX_ELEVATION, None),
        ]

    def get_climate_data(self, options):
        """Update climate data."""
        return [
            self.hass,
            options.get(CONF_TEMP_ENTITY),
            options.get(CONF_TEMP_LOW),
            options.get(CONF_TEMP_HIGH),
            options.get(CONF_PRESENCE_ENTITY),
            options.get(CONF_WEATHER_ENTITY),
            options.get(CONF_WEATHER_STATE),
            options.get(CONF_OUTSIDETEMP_ENTITY),
            self._temp_toggle,
            self._cover_type,
            options.get(CONF_TRANSPARENT_BLIND),
            options.get(CONF_LUX_ENTITY),
            options.get(CONF_IRRADIANCE_ENTITY),
            options.get(CONF_LUX_THRESHOLD),
            options.get(CONF_IRRADIANCE_THRESHOLD),
            self._lux_toggle,
            self._irradiance_toggle,
        ]

    def climate_mode_data(self, options, cover_data):
        """Update climate mode data and control method."""
        climate = ClimateCoverData(*self.get_climate_data(options))
        self.climate_state = round(ClimateCoverState(cover_data, climate).get_state())
        self.climate_state_pos = round(
            ClimateCoverState(cover_data, climate).get_state_pos()
        )
        climate_data = ClimateCoverState(cover_data, climate).climate_data
        self.control_method = "intermediate"
        if climate_data.is_summer and self.switch_mode:
            self.control_method = "summer"
        if climate_data.is_winter and self.switch_mode:
            self.control_method = "winter"

    def vertical_data(self, options):
        """Update data for vertical blinds."""
        return [
            options.get(CONF_DISTANCE),
            options.get(CONF_HEIGHT_WIN),
        ]

    def horizontal_data(self, options):
        """Update data for horizontal blinds."""
        return [
            options.get(CONF_LENGTH_AWNING),
            options.get(CONF_AWNING_ANGLE),
        ]

    def tilt_data(self, options):
        """Update data for tilted blinds."""
        return [
            options.get(CONF_TILT_DISTANCE),
            options.get(CONF_TILT_DEPTH),
            options.get(CONF_TILT_MODE),
        ]

    @property
    def state(self) -> int:
        """Handle the output of the state based on mode."""
        state = self.default_state
        if self._switch_mode:
            state = self.climate_state

        if self._use_interpolation:
            state = self.interpolate_states(state)

        if self._inverse_state and self._use_interpolation:
            _LOGGER.info(
                "Inverse state is not supported with interpolation, you can inverse the state by arranging the list from high to low"
            )

        if self._inverse_state and not self._use_interpolation:
            state = inverse_state(state)

        if self.is_wakeup_time:
            state = self.calc_wakeup_state(state)

        _LOGGER.debug("Calculated position: %s", state)
        return state

    @property
    def state_pos(self) -> int:
        """Handle the output of the state based on mode."""
        state = self.default_state_pos
        if self._switch_mode:
            state = self.climate_state_pos

        _LOGGER.debug("Calculated venetian position: %s", state)
        return state

    def interpolate_states(self, state):
        """Interpolate states."""
        normal_range = [0, 100]
        new_range = []
        if self.start_value and self.end_value:
            new_range = [self.start_value, self.end_value]
        if self.normal_list and self.new_list:
            normal_range = list(map(int, self.normal_list))
            new_range = list(map(int, self.new_list))
        if new_range:
            state = np.interp(state, normal_range, new_range)
            if state == new_range[0]:
                state = 0
            if state == new_range[-1]:
                state = 100
        return state

    def calc_wakeup_state(self, state) -> int:
        """Calcs position during soft wakeup time"""
        passed_time = self.how_much_after_start_time
        options = self.config_entry.options
        open_speed = options.get(CONF_SUNRISE_OPEN_SPEED, 0)
        if open_speed == 0:
            return state
        return 100 / open_speed * passed_time.seconds / 60 / 60 * 100

    @property
    def switch_mode(self):
        """Let switch toggle climate mode."""
        return self._switch_mode

    @switch_mode.setter
    def switch_mode(self, value):
        self._switch_mode = value

    @property
    def temp_toggle(self):
        """Let switch toggle between inside or outside temperature."""
        return self._temp_toggle

    @temp_toggle.setter
    def temp_toggle(self, value):
        self._temp_toggle = value

    @property
    def control_toggle(self):
        """Toggle automation."""
        return self._control_toggle

    @control_toggle.setter
    def control_toggle(self, value):
        self._control_toggle = value

    @property
    def manual_toggle(self):
        """Toggle automation."""
        return self._manual_toggle

    @manual_toggle.setter
    def manual_toggle(self, value):
        self._manual_toggle = value

    @property
    def lux_toggle(self):
        """Toggle automation."""
        return self._lux_toggle

    @lux_toggle.setter
    def lux_toggle(self, value):
        self._lux_toggle = value

    @property
    def irradiance_toggle(self):
        """Toggle automation."""
        return self._irradiance_toggle

    @irradiance_toggle.setter
    def irradiance_toggle(self, value):
        self._irradiance_toggle = value


class AdaptiveCoverManager:
    """Track position changes."""

    def __init__(self, reset_duration: dict[str:int]) -> None:
        """Initialize the AdaptiveCoverManager."""
        self.covers: set[str] = set()

        self.manual_control: dict[str, bool] = {}
        self.manual_control_time: dict[str, dt.datetime] = {}
        self.reset_duration = dt.timedelta(**reset_duration)

    def add_covers(self, entity):
        """Update set with entities."""
        self.covers.update(entity)

    def handle_state_change(
        self,
        states_data,
        our_state,
        blind_type,
        allow_reset,
        wait_target_call,
        manual_threshold,
    ):
        """Process state change event."""
        event = states_data
        if event is None:
            return
        entity_id = event.entity_id
        if entity_id not in self.covers:
            return
        if wait_target_call.get(entity_id):
            return

        new_state = event.new_state

        if blind_type == "cover_tilt":
            new_position = new_state.attributes.get(ATTR_CURRENT_TILT_POSITION)
        else:
            new_position = new_state.attributes.get(ATTR_CURRENT_POSITION)

        if new_position != our_state:
            if (
                manual_threshold is not None
                and abs(our_state - new_position) < manual_threshold
            ):
                _LOGGER.debug(
                    "Position change is less than threshold %s for %s",
                    manual_threshold,
                    entity_id,
                )
                return
            _LOGGER.debug(
                "Set manual control for %s, for at least %s seconds, reset_allowed: %s",
                entity_id,
                self.reset_duration.total_seconds(),
                allow_reset,
            )
            self.mark_manual_control(entity_id)
            self.set_last_updated(entity_id, new_state, allow_reset)

    def handle_state_change2(
        self,
        states_data,
        our_position,
        our_tilt,
        blind_type,
        allow_reset,
        wait_target_call,
        manual_threshold,
    ):
        """Process state change event."""
        event = states_data
        if event is None:
            return
        entity_id = event.entity_id
        if entity_id not in self.covers:
            return
        if wait_target_call.get(entity_id):
            return

        new_state = event.new_state

        if blind_type == "cover_tilt":
            new_tilt = new_state.attributes.get(ATTR_CURRENT_TILT_POSITION)
            new_position = new_state.attributes.get(ATTR_CURRENT_POSITION)
        else:
            new_position = new_state.attributes.get(ATTR_CURRENT_POSITION)

        diff = 0
        if new_tilt is not None:
            diff = abs(our_tilt - new_tilt)

        if new_position is not None:
            diff = diff + abs(our_position - new_position)

        if diff > 0:
            if manual_threshold is not None and diff < manual_threshold:
                _LOGGER.debug(
                    "Position change is less than threshold %s for %s",
                    manual_threshold,
                    entity_id,
                )
                return
            _LOGGER.debug(
                "Set manual control for %s, for at least %s seconds, reset_allowed: %s",
                entity_id,
                self.reset_duration.total_seconds(),
                allow_reset,
            )
            self.mark_manual_control(entity_id)
            self.set_last_updated(entity_id, new_state, allow_reset)

    def set_last_updated(self, entity_id, new_state, allow_reset):
        """Set last updated time for manual control."""
        if entity_id not in self.manual_control_time or allow_reset:
            last_updated = new_state.last_updated
            self.manual_control_time[entity_id] = last_updated
            _LOGGER.debug(
                "Updating last updated to %s for %s. Allow reset:%s",
                last_updated,
                entity_id,
                allow_reset,
            )
        elif not allow_reset:
            _LOGGER.debug(
                "Already time specified for %s, reset is not allowed by user setting:%s",
                entity_id,
                allow_reset,
            )

    def mark_manual_control(self, cover: str) -> None:
        """Mark cover as under manual control."""
        self.manual_control[cover] = True

    async def reset_if_needed(self):
        """Reset manual control state of the covers."""
        current_time = dt.datetime.now(dt.UTC)
        manual_control_time_copy = dict(self.manual_control_time)
        for entity_id, last_updated in manual_control_time_copy.items():
            if current_time - last_updated > self.reset_duration:
                _LOGGER.debug(
                    "Resetting manual override for %s, because duration has elapsed",
                    entity_id,
                )
                self.reset(entity_id)

    def reset(self, entity_id):
        """Reset manual control for a cover."""
        self.manual_control[entity_id] = False
        self.manual_control_time.pop(entity_id, None)
        _LOGGER.debug("Reset manual override for %s", entity_id)

    def is_cover_manual(self, entity_id):
        """Check if a cover is under manual control."""
        return self.manual_control.get(entity_id, False)

    @property
    def binary_cover_manual(self):
        """Check if any cover is under manual control."""
        return any(value for value in self.manual_control.values())

    @property
    def manual_controlled(self):
        """Get the list of covers under manual control."""
        return [k for k, v in self.manual_control.items() if v]


def inverse_state(state: int) -> int:
    """Inverse state."""
    return 100 - state
