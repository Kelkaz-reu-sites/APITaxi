import hmac

from flask import Blueprint, current_app, jsonify, request, Response

from APITaxi2 import observability


blueprint = Blueprint('internal_metrics', __name__)


@blueprint.route('/internal/metrics', methods=['GET'])
def metrics():
    expected_key = current_app.config.get('INTERNAL_HEALTHCHECK_KEY')
    provided_key = request.headers.get('X-Internal-Key', '')

    if not expected_key:
        return jsonify({'status': 'unconfigured'}), 503

    if not hmac.compare_digest(provided_key, expected_key):
        return jsonify({'errors': {'': ['Invalid internal key.']}}), 401

    return Response(
        observability.render_metrics(),
        mimetype='text/plain; version=0.0.4; charset=utf-8',
    )
