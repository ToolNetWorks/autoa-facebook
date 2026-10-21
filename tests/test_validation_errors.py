"""The backend answers with a sentence, not with the report of the validator.

A missing workspace id must never reach the backend as the text "undefined",
and a validation error must never show the technical report of Pydantic to a
customer.
"""
import json

from test_app import client  # noqa: F401 (registers the client fixture)


def test_missing_id_in_path_answers_with_a_sentence(client):
    response = client.get('/api/admin/pipelines/abc/key')
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body['detail'], str)
    assert body['detail']
    assert 'Input should be a valid integer' not in json.dumps(body)
    assert 'unable to parse string' not in json.dumps(body)
    assert body.get('fields') == ['pipeline_id']


def test_missing_id_in_query_answers_with_a_sentence(client):
    response = client.get('/api/accounts?pipeline_id=abc')
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body['detail'], str)
    assert 'Input should be a valid integer' not in json.dumps(body)


def test_wrong_body_type_answers_with_a_sentence(client):
    response = client.post('/api/pipelines', json={'name': 42})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body['detail'], str)
    assert 'Input should be a valid' not in json.dumps(body)


def test_a_valid_request_is_not_affected(client):
    created = client.post('/api/pipelines', json={'name': '@validation_ok'})
    assert created.status_code == 201, created.text
    pipeline_id = created.json()['id']
    assert client.get(f'/api/admin/pipelines/{pipeline_id}/key').status_code == 200
