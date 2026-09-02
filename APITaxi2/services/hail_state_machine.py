from dataclasses import dataclass

from APITaxi_models2.hail import HAIL_TERMINAL_STATUS

from .. import activity_logs, processes


# TRANSITIONS define the permissions required to go from a state to another.
# Top level keys are the origin hail status. Subkeys are the possible future
# states, and values are the permission required to perform the transition.
TRANSITIONS = {
    'received': {
        'declined_by_customer': 'moteur',
    },
    'sent_to_operator': {
        'declined_by_customer': 'moteur',
    },
    'received_by_operator': {
        'declined_by_customer': 'moteur',
        'received_by_taxi': 'operateur',
    },
    'received_by_taxi': {
        'accepted_by_taxi': 'operateur',
        'declined_by_taxi': 'operateur',
        'incident_taxi': 'operateur',
        'incident_customer': 'moteur',
        'declined_by_customer': 'moteur',
    },
    'accepted_by_taxi': {
        'incident_customer': 'moteur',
        'declined_by_customer': 'moteur',
        'accepted_by_customer': 'moteur',
        'incident_taxi': 'operateur',
    },
    'accepted_by_customer': {
        'customer_on_board': 'operateur',
        'incident_customer': 'moteur',
        'incident_taxi': 'operateur',
    },
    'customer_on_board': {
        'incident_customer': 'moteur',
        'incident_taxi': 'operateur',
        'finished': 'operateur',
    }
}


# Keys are the new hail status, values the new taxi status.
#
# Rezo D18: an explicit refusal leaves the taxi available. Refusing a ride that
# is too far away must not cost the driver their availability; upstream le.taxi
# set 'off' here. The hail is redistributed to another taxi either way, and the
# refusing driver is excluded from that redistribution.
TAXI_STATUS_BY_HAIL_STATUS = {
    'accepted_by_customer': 'oncoming',
    'declined_by_customer': 'free',
    'declined_by_taxi': 'free',
    'customer_on_board': 'occupied',
    'incident_taxi': 'free',
    'incident_customer': 'free',
    'finished': 'free',
}


@dataclass(frozen=True)
class TransitionResult:
    changed: bool
    event_replayed: bool = False


@dataclass(frozen=True)
class TimeoutSpec:
    initial_hail_status: str
    new_hail_status: str
    new_taxi_status: str
    countdown_config_key: str

    def countdown(self, app_config):
        return app_config[self.countdown_config_key]


TIMEOUTS_BY_STATUS = {
    'received_by_operator': TimeoutSpec(
        initial_hail_status='received_by_operator',
        new_hail_status='failure',
        new_taxi_status='free',
        countdown_config_key='REZO_TAXI_OPERATOR_ACK_TIMEOUT_SECONDS',
    ),
    # Rezo D19: silence pauses the driver, it does not end their shift. An
    # unanswered hail signals a driver momentarily unreachable, so they stop
    # receiving hails without leaving the service, and go available again
    # explicitly. Upstream le.taxi set 'off' here.
    'received_by_taxi': TimeoutSpec(
        initial_hail_status='received_by_taxi',
        new_hail_status='timeout_taxi',
        new_taxi_status='occupied',
        countdown_config_key='REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS',
    ),
    'accepted_by_taxi': TimeoutSpec(
        initial_hail_status='accepted_by_taxi',
        new_hail_status='timeout_customer',
        new_taxi_status='free',
        countdown_config_key='REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS',
    ),
    'accepted_by_customer': TimeoutSpec(
        initial_hail_status='accepted_by_customer',
        new_hail_status='timeout_accepted_by_customer',
        new_taxi_status='occupied',
        countdown_config_key='REZO_TAXI_PICKUP_TIMEOUT_SECONDS',
    ),
    'customer_on_board': TimeoutSpec(
        initial_hail_status='customer_on_board',
        new_hail_status='timeout_taxi',
        new_taxi_status='off',
        countdown_config_key='REZO_TAXI_RIDE_TIMEOUT_SECONDS',
    ),
}


def _has_role(user, role):
    return bool(user and user.has_role(role))


def _has_processed_event(hail, event_id):
    if not event_id:
        return False
    return any(
        transition.get('event_id') == event_id
        for transition in (hail.transition_log or [])
    )


def set_taxi_status(hail, vehicle_description, new_taxi_status, **log_extra):
    old_taxi_status = vehicle_description.status
    vehicle_description.status = new_taxi_status
    activity_logs.log_taxi_status(
        hail.taxi_id,
        old_taxi_status,
        new_taxi_status,
        **log_extra,
    )


def apply_transition(
    hail,
    vehicle_description,
    new_status,
    user=None,
    taxi_phone_number=None,
    event_id=None,
):
    """Apply a user-driven hail status transition and related taxi side effects.

    Replaying the same event_id is idempotent: the hail is not mutated again.
    Re-applying the current status is also treated as a no-op for historical API
    compatibility.
    """
    if _has_processed_event(hail, event_id):
        return TransitionResult(changed=False, event_replayed=True)

    if hail.status == new_status:
        return TransitionResult(changed=False)

    if hail.status in HAIL_TERMINAL_STATUS:
        raise ValueError(f'Hail status is {hail.status} and cannot be changed to {new_status}')

    if hail.status in TRANSITIONS:
        allowed_transitions = TRANSITIONS[hail.status]
        if new_status not in allowed_transitions:
            raise ValueError(f'Impossible to set status from {hail.status} to {new_status}')

        required_role = allowed_transitions[new_status]
        if not _has_role(user, required_role) and not _has_role(user, 'admin'):
            raise ValueError(
                f'Permission {required_role} is required to change status '
                f'from {hail.status} to {new_status}'
            )

    # There are two ways to provide the taxi phone number:
    # - when we call the operator's API to request the taxi, it can be returned
    # - or it needs to be provided when the taxi accepts the trip.
    if new_status == 'accepted_by_taxi':
        if taxi_phone_number:
            hail.taxi_phone_number = taxi_phone_number
        elif not hail.taxi_phone_number:
            raise ValueError('Status changes to accepted_by_taxi but taxi_phone_number is not provided')

    processes.change_status(hail, new_status, user=user, event_id=event_id)

    new_taxi_status = TAXI_STATUS_BY_HAIL_STATUS.get(new_status)
    if new_taxi_status:
        set_taxi_status(
            hail,
            vehicle_description,
            new_taxi_status,
            hail_id=hail.id,
        )

    return TransitionResult(changed=True)


def apply_timeout_transition(
    hail,
    vehicle_description,
    new_hail_status,
    new_taxi_status=None,
    reason='timeout',
):
    processes.change_status(hail, new_hail_status, reason=reason)

    if new_taxi_status:
        set_taxi_status(
            hail,
            vehicle_description,
            new_taxi_status,
            task='handle_hail_timeout',
        )


def timeout_for_status(status):
    return TIMEOUTS_BY_STATUS.get(status)
