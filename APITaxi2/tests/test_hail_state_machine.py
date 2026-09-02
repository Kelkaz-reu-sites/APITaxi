import pytest

from APITaxi_models2 import db, VehicleDescription
from APITaxi_models2.unittest.factories import HailFactory

from APITaxi2.services import hail_state_machine


def _vehicle_description_for(hail):
    return VehicleDescription.query.filter_by(
        vehicle_id=hail.taxi.vehicle_id,
        added_by_id=hail.operateur_id,
    ).one()


def test_hail_lifecycle_path(operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_operator',
    )
    vehicle_description = _vehicle_description_for(hail)

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'received_by_taxi',
        user=operateur.user,
    )
    assert result.changed is True
    assert hail.status == 'received_by_taxi'
    assert vehicle_description.status == 'free'

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'accepted_by_taxi',
        user=operateur.user,
        taxi_phone_number='+262692000000',
    )
    assert result.changed is True
    assert hail.status == 'accepted_by_taxi'
    assert hail.taxi_phone_number == '+262692000000'

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'accepted_by_customer',
        user=moteur.user,
    )
    assert result.changed is True
    assert hail.status == 'accepted_by_customer'
    assert vehicle_description.status == 'oncoming'

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'customer_on_board',
        user=operateur.user,
    )
    assert result.changed is True
    assert hail.status == 'customer_on_board'
    assert vehicle_description.status == 'occupied'

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'finished',
        user=operateur.user,
    )
    assert result.changed is True
    assert hail.status == 'finished'
    assert vehicle_description.status == 'free'
    db.session.commit()


def test_driver_refusal_keeps_taxi_available(operateur, moteur):
    """Rezo D18: refusing a ride must not cost the driver their availability.

    A driver refuses because the pickup is too far, not because they are ending
    their shift. The hail is redistributed to another taxi either way, and the
    refusing driver is excluded from that redistribution.
    """
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
    )
    vehicle_description = _vehicle_description_for(hail)

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'declined_by_taxi',
        user=operateur.user,
    )

    assert result.changed is True
    assert hail.status == 'declined_by_taxi'
    assert vehicle_description.status == 'free'
    db.session.commit()


def test_invalid_transition_is_refused(operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received',
    )
    vehicle_description = _vehicle_description_for(hail)

    with pytest.raises(ValueError) as exc:
        hail_state_machine.apply_transition(
            hail,
            vehicle_description,
            'accepted_by_taxi',
            user=operateur.user,
            taxi_phone_number='+262692000000',
        )

    assert str(exc.value) == 'Impossible to set status from received to accepted_by_taxi'
    assert hail.status == 'received'


def test_permission_is_required(operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_operator',
    )
    vehicle_description = _vehicle_description_for(hail)

    with pytest.raises(ValueError) as exc:
        hail_state_machine.apply_transition(
            hail,
            vehicle_description,
            'received_by_taxi',
            user=moteur.user,
        )

    assert str(exc.value) == (
        'Permission operateur is required to change status from '
        'received_by_operator to received_by_taxi'
    )
    assert hail.status == 'received_by_operator'


def test_terminal_transition_is_refused(operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='finished',
    )
    vehicle_description = _vehicle_description_for(hail)

    with pytest.raises(ValueError) as exc:
        hail_state_machine.apply_transition(
            hail,
            vehicle_description,
            'received_by_taxi',
            user=operateur.user,
        )

    assert str(exc.value) == 'Hail status is finished and cannot be changed to received_by_taxi'
    assert hail.status == 'finished'


def test_same_status_replay_is_noop(operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
    )
    vehicle_description = _vehicle_description_for(hail)
    log_size = len(hail.transition_log or [])

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'received_by_taxi',
        user=operateur.user,
    )

    assert result.changed is False
    assert result.event_replayed is False
    assert hail.status == 'received_by_taxi'
    assert len(hail.transition_log or []) == log_size


def test_event_id_replay_is_idempotent(operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
    )
    vehicle_description = _vehicle_description_for(hail)

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'accepted_by_taxi',
        user=operateur.user,
        taxi_phone_number='+262692000000',
        event_id='evt-accept-driver',
    )
    assert result.changed is True
    log_size = len(hail.transition_log)

    result = hail_state_machine.apply_transition(
        hail,
        vehicle_description,
        'accepted_by_customer',
        user=moteur.user,
        event_id='evt-accept-driver',
    )

    assert result.changed is False
    assert result.event_replayed is True
    assert hail.status == 'accepted_by_taxi'
    assert len(hail.transition_log) == log_size
    db.session.commit()


def test_timeout_transition_sets_status_and_taxi_side_effect(operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='accepted_by_taxi',
    )
    vehicle_description = _vehicle_description_for(hail)
    vehicle_description.status = 'oncoming'

    hail_state_machine.apply_timeout_transition(
        hail,
        vehicle_description,
        'timeout_customer',
        new_taxi_status='free',
    )

    assert hail.status == 'timeout_customer'
    assert vehicle_description.status == 'free'
    assert hail.transition_log[-1]['reason'] == 'timeout'
    assert hail.transition_log[-1]['user'] is None
    db.session.commit()


def test_timeout_specs_use_rezo_configuration(app):
    """Rezo D19: silence pauses the driver, it does not end their shift.

    An unanswered hail signals a driver momentarily unreachable — phone in a
    pocket, hands on the wheel. They stop receiving hails without leaving the
    service, and must explicitly go available again.
    """
    timeout = hail_state_machine.timeout_for_status('received_by_taxi')

    assert timeout.initial_hail_status == 'received_by_taxi'
    assert timeout.new_hail_status == 'timeout_taxi'
    assert timeout.new_taxi_status == 'occupied'
    assert timeout.countdown(app.config) == 120

    assert hail_state_machine.timeout_for_status('finished') is None
