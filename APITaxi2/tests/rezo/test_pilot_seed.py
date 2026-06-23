import json

from APITaxi2.commands.rezo_pilot import DEFAULT_REZO_SERVICE_ACCOUNT_EMAIL
from APITaxi_models2 import ADS, Driver, Taxi, Vehicle, VehicleDescription
from APITaxi_models2.unittest.factories import (
    DepartementFactory,
    RoleFactory,
    RolesUsersFactory,
    TownFactory,
    UserFactory,
    ZUPCFactory,
)


def _create_rezo_service_account():
    user = UserFactory(email=DEFAULT_REZO_SERVICE_ACCOUNT_EMAIL)
    for role_name in ('moteur', 'operateur'):
        RolesUsersFactory(user=user, role=RoleFactory(name=role_name))
    return user


def _create_reunion_prerequisites(*, with_zupc=True):
    DepartementFactory(numero='974', nom='La Reunion')
    town = TownFactory(insee='97411', name='Saint-Denis')
    if with_zupc:
        ZUPCFactory(nom='REZO_REUNION_MVP', allowed=[town])
    return town


def _pilot_payload():
    return {
        'driver': {
            'first_name': 'Rezo',
            'last_name': 'Pilot',
            'birth_date': None,
            'professional_licence': 'REZO-PILOT-LIC-001',
            'departement': {'numero': '974'},
        },
        'vehicle': {
            'licence_plate': 'RZ-001-AA',
            'constructor': 'Toyota',
            'model': 'Prius',
            'engine': 'hybrid',
            'color': 'white',
            'nb_seats': 4,
            'credit_card_accepted': True,
        },
        'ads': {
            'numero': 'REZO-PILOT-ADS-001',
            'insee': '97411',
            'owner_type': 'individual',
            'owner_name': 'Rezo Pilot',
            'category': 'PILOT',
        },
        'taxi': {
            'status': 'off',
            'radius': 5000,
        },
    }


def _write_pilot_file(path):
    path.write_text(json.dumps(_pilot_payload()), encoding='utf-8')
    return path


def _summary(result):
    return json.loads(result.output.splitlines()[0])


def test_rezo_seed_pilot_taxi_dry_run_rolls_back(app, tmp_path):
    _create_rezo_service_account()
    _create_reunion_prerequisites()
    pilot_file = _write_pilot_file(tmp_path / 'pilot.json')

    result = app.test_cli_runner().invoke(args=[
        'rezo_seed_pilot_taxi',
        '--pilot-file',
        str(pilot_file),
    ])

    assert result.exit_code == 0, result.output
    summary = _summary(result)
    assert summary['mode'] == 'dry-run'
    assert summary['persisted'] is False
    assert summary['driver_status'] == 201
    assert summary['vehicle_status'] == 201
    assert summary['ads_status'] == 201
    assert summary['taxi_status'] == 201
    assert summary['taxi_get_status'] == 200
    assert summary['commit_calls_intercepted'] == 5
    assert 'Dry-run only, database unchanged.' in result.output

    assert Driver.query.filter_by(professional_licence='REZO-PILOT-LIC-001').count() == 0
    assert Vehicle.query.filter_by(licence_plate='RZ-001-AA').count() == 0
    assert ADS.query.filter_by(numero='REZO-PILOT-ADS-001').count() == 0
    assert Taxi.query.count() == 0


def test_rezo_seed_pilot_taxi_apply_is_idempotent(app, tmp_path):
    _create_rezo_service_account()
    _create_reunion_prerequisites()
    pilot_file = _write_pilot_file(tmp_path / 'pilot.json')
    runner = app.test_cli_runner()

    first = runner.invoke(args=[
        'rezo_seed_pilot_taxi',
        '--pilot-file',
        str(pilot_file),
        '--apply',
    ])

    assert first.exit_code == 0, first.output
    first_summary = _summary(first)
    assert first_summary['mode'] == 'apply'
    assert first_summary['persisted'] is True
    assert first_summary['driver_status'] == 201
    assert first_summary['vehicle_status'] == 201
    assert first_summary['ads_status'] == 201
    assert first_summary['taxi_status'] == 201
    assert first_summary['taxi_update_status'] == 200
    assert first_summary['taxi_get_status'] == 200
    assert first_summary['driver_count_for_service_account'] == 1
    assert first_summary['vehicle_count_for_plate'] == 1
    assert first_summary['ads_count_for_service_account'] == 1
    assert first_summary['taxi_count_for_service_account'] == 1

    taxi = Taxi.query.one()
    vehicle_description = VehicleDescription.query.filter_by(vehicle_id=taxi.vehicle_id).one()
    assert taxi.id == first_summary['taxi_id']
    assert vehicle_description.status == 'off'
    assert vehicle_description.radius == 5000

    second = runner.invoke(args=[
        'rezo_seed_pilot_taxi',
        '--pilot-file',
        str(pilot_file),
        '--apply',
    ])

    assert second.exit_code == 0, second.output
    second_summary = _summary(second)
    assert second_summary['driver_status'] == 200
    assert second_summary['vehicle_status'] == 200
    assert second_summary['ads_status'] == 200
    assert second_summary['taxi_status'] == 200
    assert second_summary['taxi_id'] == first_summary['taxi_id']
    assert Driver.query.filter_by(professional_licence='REZO-PILOT-LIC-001').count() == 1
    assert Vehicle.query.filter_by(licence_plate='RZ-001-AA').count() == 1
    assert ADS.query.filter_by(numero='REZO-PILOT-ADS-001').count() == 1
    assert Taxi.query.count() == 1


def test_rezo_seed_pilot_taxi_requires_zupc_for_ads_town(app, tmp_path):
    _create_rezo_service_account()
    _create_reunion_prerequisites(with_zupc=False)
    pilot_file = _write_pilot_file(tmp_path / 'pilot.json')

    result = app.test_cli_runner().invoke(args=[
        'rezo_seed_pilot_taxi',
        '--pilot-file',
        str(pilot_file),
        '--apply',
    ])

    assert result.exit_code != 0
    assert 'exists but is not attached to any ZUPC' in result.output
    assert Driver.query.filter_by(professional_licence='REZO-PILOT-LIC-001').count() == 0
    assert Vehicle.query.filter_by(licence_plate='RZ-001-AA').count() == 0
    assert ADS.query.filter_by(numero='REZO-PILOT-ADS-001').count() == 0
    assert Taxi.query.count() == 0
