"""Rezo-specific pilot taxi preparation commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import click
from flask import Blueprint, current_app
from sqlalchemy.orm import joinedload

from APITaxi_models2 import ADS, Driver, Taxi, Town, User, Vehicle, db


blueprint = Blueprint('commands_rezo_pilot', __name__, cli_group=None)

DEFAULT_REZO_SERVICE_ACCOUNT_EMAIL = 'rezo-taxi-live-service@rezo.re'
REQUIRED_SERVICE_ACCOUNT_ROLES = {'moteur', 'operateur'}


class PathlibPath(click.Path):
    """click.Path does not convert to a Path object."""

    def convert(self, *args):
        return Path(super().convert(*args))


PILOT_FILE = PathlibPath(exists=True, dir_okay=False, path_type=str)


def _load_pilot_file(path: Path):
    with path.open(encoding='utf-8') as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise click.ClickException('Pilot file must contain a JSON object')

    for key in ('driver', 'vehicle', 'ads'):
        if not isinstance(payload.get(key), dict):
            raise click.ClickException(f'Pilot file must contain an object `{key}`')

    return payload


def _service_account(email):
    user = User.query.options(joinedload(User.roles)).filter_by(email=email).one_or_none()
    if user is None:
        raise click.ClickException(f'Rezo service account not found: {email}')

    roles = {role.name for role in user.roles}
    missing_roles = sorted(REQUIRED_SERVICE_ACCOUNT_ROLES - roles)
    if missing_roles:
        raise click.ClickException(
            'Rezo service account is missing required role(s): %s'
            % ', '.join(missing_roles)
        )

    return user, sorted(roles)


def _driver_departement_numero(driver):
    departement = driver.get('departement')
    if not isinstance(departement, dict) or not departement.get('numero'):
        raise click.ClickException(
            'Pilot driver must contain `departement.numero`, for example "974"'
        )
    return str(departement['numero'])


def _validate_geography(insee):
    town = Town.query.options(joinedload(Town.allowed)).filter_by(insee=insee).one_or_none()
    if town is None:
        raise click.ClickException(f'Pilot ADS INSEE {insee} does not exist in Town')

    if not town.allowed:
        raise click.ClickException(
            f'Pilot ADS INSEE {insee} exists but is not attached to any ZUPC'
        )

    return town, sorted(zupc.nom for zupc in town.allowed)


def _call_json(client, method, path, payload=None, expected_statuses=(200, 201)):
    response = getattr(client, method)(path, json=payload)
    response_payload = response.get_json(silent=True) or {}
    if response.status_code not in expected_statuses:
        raise click.ClickException(
            'Unexpected response from %s %s: HTTP %s %s'
            % (method.upper(), path, response.status_code, json.dumps(response_payload, sort_keys=True))
        )

    data = response_payload.get('data') or []
    if not data:
        raise click.ClickException(
            'Unexpected empty response from %s %s' % (method.upper(), path)
        )
    return response.status_code, data[0]


def _pilot_taxi_summary(pilot, service_account_email, apply=False):
    driver = dict(pilot['driver'])
    vehicle = dict(pilot['vehicle'])
    ads = dict(pilot['ads'])
    taxi_settings = dict(pilot.get('taxi') or {})

    departement_numero = _driver_departement_numero(driver)
    town, zupc_names = _validate_geography(str(ads.get('insee', '')))
    user, roles = _service_account(service_account_email)

    summary = {
        'mode': 'apply' if apply else 'dry-run',
        'service_account_email': user.email,
        'service_account_roles': roles,
        'ads_insee': town.insee,
        'ads_town': town.name,
        'zupc': zupc_names,
        'commit_calls_intercepted': 0,
    }

    def flush_instead_of_commit():
        summary['commit_calls_intercepted'] += 1
        db.session.flush()

    try:
        with mock.patch.object(db.session, 'commit', side_effect=flush_instead_of_commit):
            with current_app.test_client() as client:
                client.environ_base['HTTP_X_API_KEY'] = user.apikey

                status, _created_driver = _call_json(
                    client,
                    'post',
                    '/drivers',
                    {'data': [driver]},
                )
                summary['driver_status'] = status

                status, created_vehicle = _call_json(
                    client,
                    'post',
                    '/vehicles',
                    {'data': [vehicle]},
                )
                summary['vehicle_status'] = status

                ads['vehicle_id'] = created_vehicle['id']
                status, _created_ads = _call_json(
                    client,
                    'post',
                    '/ads',
                    {'data': [ads]},
                )
                summary['ads_status'] = status

                taxi_payload = {
                    'driver': {
                        'professional_licence': driver['professional_licence'],
                        'departement': departement_numero,
                    },
                    'vehicle': {
                        'licence_plate': vehicle['licence_plate'],
                    },
                    'ads': {
                        'numero': ads['numero'],
                        'insee': ads['insee'],
                    },
                }
                status, created_taxi = _call_json(
                    client,
                    'post',
                    '/taxis',
                    {'data': [taxi_payload]},
                )
                summary['taxi_status'] = status
                summary['taxi_id'] = created_taxi['id']

                taxi_update = {
                    key: taxi_settings[key]
                    for key in ('status', 'radius')
                    if key in taxi_settings
                }
                if taxi_update:
                    status, _updated_taxi = _call_json(
                        client,
                        'put',
                        f"/taxis/{created_taxi['id']}",
                        {'data': [taxi_update]},
                        expected_statuses=(200,),
                    )
                    summary['taxi_update_status'] = status

                status, _taxi_detail = _call_json(
                    client,
                    'get',
                    f"/taxis/{created_taxi['id']}",
                    expected_statuses=(200,),
                )
                summary['taxi_get_status'] = status

        if apply:
            db.session.commit()
            summary['persisted'] = True
        else:
            db.session.rollback()
            summary['persisted'] = False
    except Exception:
        db.session.rollback()
        raise

    summary['driver_count_for_service_account'] = Driver.query.filter(
        Driver.professional_licence == driver['professional_licence'],
        Driver.added_by == user,
    ).count()
    summary['vehicle_count_for_plate'] = Vehicle.query.filter(
        Vehicle.licence_plate == vehicle['licence_plate'],
    ).count()
    summary['ads_count_for_service_account'] = ADS.query.filter(
        ADS.numero == ads['numero'],
        ADS.insee == ads['insee'],
        ADS.added_by == user,
    ).count()
    summary['taxi_count_for_service_account'] = Taxi.query.filter(
        Taxi.added_by == user,
    ).count()

    return summary


@blueprint.cli.command('rezo_seed_pilot_taxi')
@click.option(
    '--pilot-file',
    required=True,
    type=PILOT_FILE,
    help='JSON file containing the pilot driver, vehicle, ADS and optional taxi settings.',
)
@click.option(
    '--service-account-email',
    default=DEFAULT_REZO_SERVICE_ACCOUNT_EMAIL,
    show_default=True,
    help='APITaxi service account used to create the Rezo pilot taxi.',
)
@click.option(
    '--apply',
    is_flag=True,
    help='Persist the pilot taxi. Without this flag, all API route writes are rolled back.',
)
def rezo_seed_pilot_taxi(pilot_file, service_account_email, apply):
    """Create or validate a Rezo pilot taxi through the real APITaxi routes."""
    pilot = _load_pilot_file(pilot_file)
    summary = _pilot_taxi_summary(
        pilot,
        service_account_email=service_account_email,
        apply=apply,
    )

    click.echo(json.dumps(summary, sort_keys=True))
    if apply:
        click.echo('Pilot taxi persisted. Record the taxi_id in Rezo as taxi_core_id.')
    else:
        click.echo('Dry-run only, database unchanged.')
