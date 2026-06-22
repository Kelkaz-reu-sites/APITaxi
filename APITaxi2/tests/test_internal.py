class TestInternalAuth:
    def test_invalid(self, anonymous, moteur):
        resp = anonymous.client.post('/internal/auth', json={'data': [{}]})
        assert resp.status_code == 400
        assert 'email' in resp.json['errors']['data']['0']

        # Should specify either apikey or password
        resp = anonymous.client.post('/internal/auth', json={'data': [{
            'email': 'xxx',
        }]})
        assert resp.status_code == 400

        # Specify both password and apikey
        resp = anonymous.client.post('/internal/auth', json={'data': [{
            'email': 'xxx',
            'apikey': 'xxx',
            'password': 'xxx',
        }]})
        assert resp.status_code == 400

        # Invalid password
        resp = moteur.client.post('/internal/auth', json={'data': [{
            'email': moteur.user.email,
            'password': moteur.user.password + '_invalid'
        }]})
        assert resp.status_code == 401

        # Invalid api key
        resp = moteur.client.post('/internal/auth', json={'data': [{
            'email': moteur.user.email,
            'apikey': moteur.user.apikey + '_invalid'
        }]})
        assert resp.status_code == 401

    def test_ok(self, moteur, QueriesTracker):
        with QueriesTracker() as qtracker:
            resp = moteur.client.post('/internal/auth', json={'data': [{
                'email': moteur.user.email,
                'password': moteur.user.password,
            }]})
            # If we used Flask-Security (the X-Api-Key header would be found)
            # SELECT permissions, INSERT LOG (auth_apikey), SELECT user, INSERT LOG (login_password)
            # Since we use Flask-HTTPAuth, and this view is not decorated with login_required
            # SELECT user, INSERT LOG (login_password)
            assert qtracker.count == 2
        assert resp.status_code == 200

        resp = moteur.client.post('/internal/auth', json={'data': [{
            'email': moteur.user.email,
            'apikey': moteur.user.apikey,
        }]})
        assert resp.status_code == 200


class TestInternalHealth:
    def test_invalid(self, anonymous):
        resp = anonymous.client.get('/internal/health')
        assert resp.status_code == 401

        resp = anonymous.client.get('/internal/health', headers={
            'X-Internal-Key': 'invalid',
        })
        assert resp.status_code == 401

    def test_ok(self, anonymous):
        resp = anonymous.client.get('/internal/health', headers={
            'X-Internal-Key': 'test-internal-key',
        })

        assert resp.status_code == 200
        assert resp.json == {'status': 'ok'}

    def test_unconfigured(self, app, anonymous):
        app.config['INTERNAL_HEALTHCHECK_KEY'] = None

        resp = anonymous.client.get('/internal/health', headers={
            'X-Internal-Key': 'test-internal-key',
        })

        assert resp.status_code == 503
        assert resp.json == {'status': 'unconfigured'}
