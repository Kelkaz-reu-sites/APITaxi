import gzip
import json

from sqlalchemy import func

from APITaxi2.commands.rezo_reunion import (
    REUNION_TOWNS,
    REZO_REUNION_MVP_ZUPC_ID,
    REZO_REUNION_MVP_ZUPC_NAME,
)
from APITaxi_models2 import Town, ZUPC


def _square_feature(insee, index):
    lon = 55.1 + (index % 6) * 0.08
    lat = -21.3 + (index // 6) * 0.08
    size = 0.02
    return {
        'type': 'Feature',
        'properties': {
            'id': f'{insee}000AB',
            'commune': insee,
            'prefixe': '000',
            'code': 'AB',
        },
        'geometry': {
            'type': 'MultiPolygon',
            'coordinates': [[[
                [lon, lat],
                [lon + size, lat],
                [lon + size, lat + size],
                [lon, lat + size],
                [lon, lat],
            ]]],
        },
    }


def _write_sections(path, *, missing=None):
    missing = set(missing or [])
    features = [
        _square_feature(insee, index)
        for index, insee in enumerate(sorted(REUNION_TOWNS))
        if insee not in missing
    ]
    payload = {'type': 'FeatureCollection', 'features': features}

    if path.name.endswith('.gz'):
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            json.dump(payload, handle)
    else:
        path.write_text(json.dumps(payload), encoding='utf-8')

    return features


def test_rezo_import_reunion_cadastre_dry_run_does_not_write(app, tmp_path):
    sections_path = tmp_path / 'cadastre-974-sections.json.gz'
    _write_sections(sections_path)

    runner = app.test_cli_runner()
    result = runner.invoke(args=[
        'rezo_import_reunion_cadastre',
        '--cadastre-sections',
        str(sections_path),
        '--dry-run',
    ])

    assert result.exit_code == 0, result.output
    assert 'Reunion cadastre source validated: 24 towns' in result.output
    assert 'Dry-run only, database unchanged.' in result.output
    assert Town.query.count() == 0
    assert ZUPC.query.count() == 0


def test_rezo_import_reunion_cadastre_imports_towns_and_zupc(app, tmp_path):
    sections_path = tmp_path / 'cadastre-974-sections.json.gz'
    features = _write_sections(sections_path)
    saint_denis_feature = features[10]
    lon = saint_denis_feature['geometry']['coordinates'][0][0][0][0] + 0.01
    lat = saint_denis_feature['geometry']['coordinates'][0][0][0][1] + 0.01

    runner = app.test_cli_runner()
    result = runner.invoke(args=[
        'rezo_import_reunion_cadastre',
        '--cadastre-sections',
        str(sections_path),
    ])

    assert result.exit_code == 0, result.output
    assert Town.query.filter(Town.insee.like('974%')).count() == 24
    assert Town.query.filter_by(insee='97411').one().name == 'Saint-Denis'

    matching_towns = Town.query.filter(
        func.ST_Intersects(Town.shape, f'POINT({lon} {lat})')
    ).all()
    assert [town.insee for town in matching_towns] == ['97411']

    zupc = ZUPC.query.filter_by(zupc_id=REZO_REUNION_MVP_ZUPC_ID).one()
    assert zupc.nom == REZO_REUNION_MVP_ZUPC_NAME
    assert sorted(town.insee for town in zupc.allowed) == sorted(REUNION_TOWNS)

    second_result = runner.invoke(args=[
        'rezo_import_reunion_cadastre',
        '--cadastre-sections',
        str(sections_path),
    ])
    assert second_result.exit_code == 0, second_result.output
    assert Town.query.filter(Town.insee.like('974%')).count() == 24
    assert ZUPC.query.count() == 1


def test_rezo_import_reunion_cadastre_fails_when_town_missing(app, tmp_path):
    sections_path = tmp_path / 'cadastre-974-sections.json'
    _write_sections(sections_path, missing={'97424'})

    runner = app.test_cli_runner()
    result = runner.invoke(args=[
        'rezo_import_reunion_cadastre',
        '--cadastre-sections',
        str(sections_path),
    ])

    assert result.exit_code != 0
    assert 'Missing cadastre sections for Reunion INSEE codes: 97424' in result.output
    assert Town.query.count() == 0
    assert ZUPC.query.count() == 0
