import hmac

from flask import Blueprint, current_app, jsonify, request


blueprint = Blueprint('internal_health', __name__)


@blueprint.route('/internal/health', methods=['GET'])
def health():
    expected_key = current_app.config.get('INTERNAL_HEALTHCHECK_KEY')
    provided_key = request.headers.get('X-Internal-Key', '')

    if not expected_key:
        return jsonify({'status': 'unconfigured'}), 503

    if not hmac.compare_digest(provided_key, expected_key):
        return jsonify({'errors': {'': ['Invalid internal key.']}}), 401

    return jsonify({'status': 'ok'})
