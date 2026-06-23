"""Rezo-specific Reunion geographic import commands."""

from __future__ import annotations

import gzip
import json
import uuid
from pathlib import Path

import click
from flask import Blueprint
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union
from shapely.validation import make_valid

from APITaxi_models2 import db, Town, ZUPC


blueprint = Blueprint('commands_rezo_reunion', __name__, cli_group=None)


REZO_REUNION_MVP_ZUPC_ID = uuid.UUID('949cdf3e-9128-524e-a39f-db91c1ea0cc7')
REZO_REUNION_MVP_ZUPC_NAME = 'REZO_REUNION_MVP'

REUNION_TOWNS = {
    '97401': 'Les Avirons',
    '97402': 'Bras-Panon',
    '97403': 'Entre-Deux',
    '97404': "L'Etang-Sale",
    '97405': 'Petite-Ile',
    '97406': 'La Plaine-des-Palmistes',
    '97407': 'Le Port',
    '97408': 'La Possession',
    '97409': 'Saint-Andre',
    '97410': 'Saint-Benoit',
    '97411': 'Saint-Denis',
    '97412': 'Saint-Joseph',
    '97413': 'Saint-Leu',
    '97414': 'Saint-Louis',
    '97415': 'Saint-Paul',
    '97416': 'Saint-Pierre',
    '97417': 'Saint-Philippe',
    '97418': 'Sainte-Marie',
    '97419': 'Sainte-Rose',
    '97420': 'Sainte-Suzanne',
    '97421': 'Salazie',
    '97422': 'Le Tampon',
    '97423': 'Les Trois-Bassins',
    '97424': 'Cilaos',
}


class PathlibPath(click.Path):
    """click.Path does not convert to a Path object."""

    def convert(self, *args):
        return Path(super().convert(*args))


PATH = PathlibPath(exists=True, dir_okay=False, path_type=str)


def _load_json(path: Path):
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8') as handle:
            return json.load(handle)

    with path.open(encoding='utf-8') as handle:
        return json.load(handle)


def _iter_features(payload):
    if isinstance(payload, dict) and payload.get('type') == 'FeatureCollection':
        yield from payload.get('features', [])
        return

    if isinstance(payload, list):
        for record in payload:
            if isinstance(record, dict) and record.get('type') == 'Feature':
                yield record
            elif isinstance(record, dict) and record.get('geo_shape'):
                yield {
                    'type': 'Feature',
                    'properties': record,
                    'geometry': record['geo_shape'],
                }
        return

    raise click.ClickException('Unsupported cadastre JSON format')


def _valid_multipolygon(geometry):
    geometry = make_valid(geometry)
    if geometry.is_empty:
        return None

    if isinstance(geometry, Polygon):
        return MultiPolygon([geometry])

    if isinstance(geometry, MultiPolygon):
        return geometry

    polygons = []
    for item in getattr(geometry, 'geoms', []):
        if isinstance(item, Polygon):
            polygons.append(item)
        elif isinstance(item, MultiPolygon):
            polygons.extend(item.geoms)

    if not polygons:
        return None
    return MultiPolygon(polygons)


def load_reunion_town_shapes_from_cadastre_sections(path: Path):
    """Load and dissolve Etalab cadastre section features by Reunion INSEE."""
    payload = _load_json(path)
    geometries_by_insee = {insee: [] for insee in REUNION_TOWNS}

    for feature in _iter_features(payload):
        properties = feature.get('properties') or {}
        insee = str(properties.get('commune') or properties.get('code_insee') or '').strip()
        if insee not in REUNION_TOWNS:
            continue

        try:
            geometry = shape(feature['geometry'])
        except Exception as exc:
            raise click.ClickException(f'Invalid geometry for INSEE {insee}: {exc}') from exc

        geometry = _valid_multipolygon(geometry)
        if geometry is not None:
            geometries_by_insee[insee].append(geometry)

    missing = sorted(
        insee
        for insee, geometries in geometries_by_insee.items()
        if not geometries
    )
    if missing:
        raise click.ClickException(
            'Missing cadastre sections for Reunion INSEE codes: %s' % ', '.join(missing)
        )

    town_shapes = {}
    for insee, geometries in geometries_by_insee.items():
        dissolved = _valid_multipolygon(unary_union(geometries))
        if dissolved is None:
            raise click.ClickException(f'Unable to dissolve cadastre sections for INSEE {insee}')
        town_shapes[insee] = dissolved

    return town_shapes


def upsert_reunion_towns_and_zupc(town_shapes, zupc_id=None, zupc_name=None):
    """Upsert Reunion Town rows and bind them to the MVP Rezo ZUPC."""
    zupc_id = zupc_id or REZO_REUNION_MVP_ZUPC_ID
    zupc_name = zupc_name or REZO_REUNION_MVP_ZUPC_NAME

    towns = []
    for insee in sorted(REUNION_TOWNS):
        town = db.session.query(Town).filter(Town.insee == insee).one_or_none()
        if town is None:
            town = Town(insee=insee)

        town.name = REUNION_TOWNS[insee]
        town.shape = from_shape(town_shapes[insee], srid=4326)
        db.session.add(town)
        towns.append(town)

    zupc = db.session.query(ZUPC).filter(ZUPC.zupc_id == zupc_id).one_or_none()
    if zupc is None:
        zupc = ZUPC(zupc_id=zupc_id)

    zupc.nom = zupc_name
    db.session.add(zupc)
    db.session.flush()

    zupc.allowed.clear()
    zupc.allowed.extend(towns)

    db.session.commit()
    return towns, zupc


@blueprint.cli.command('rezo_import_reunion_cadastre')
@click.option(
    '--cadastre-sections',
    required=True,
    type=PATH,
    help='Etalab cadastre sections GeoJSON for department 974, plain JSON or .json.gz.',
)
@click.option(
    '--dry-run',
    is_flag=True,
    help='Parse and validate the source without writing APITaxi tables.',
)
def rezo_import_reunion_cadastre(cadastre_sections, dry_run):
    """Import Reunion Town rows and the MVP Rezo ZUPC from cadastre sections."""
    town_shapes = load_reunion_town_shapes_from_cadastre_sections(cadastre_sections)

    click.echo('Reunion cadastre source validated: %d towns' % len(town_shapes))
    if dry_run:
        click.echo('Dry-run only, database unchanged.')
        return

    towns, zupc = upsert_reunion_towns_and_zupc(town_shapes)
    click.echo(
        'Imported %d Reunion towns and bound ZUPC %s (%s).'
        % (len(towns), zupc.zupc_id, zupc.nom)
    )
