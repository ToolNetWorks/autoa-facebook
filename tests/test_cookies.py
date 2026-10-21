import json

import pytest
from test_app import client

from cookie_import import InvalidCookies, parse_cookie_file


def sample_cookies(uid='123456'):
    return {'url':'https://www.facebook.com/', 'cookies':[
        {'name':'c_user','value':uid,'domain':'.facebook.com','path':'/','httpOnly':False},
        {'name':'xs','value':'demo-do-not-use','domain':'.facebook.com','path':'/','httpOnly':True},
        {'name':'datr','value':'example','domain':'.facebook.com','path':'/','secure':True},
    ]}


def test_exported_cookie_json_is_importable():
    cookies, uid = parse_cookie_file(json.dumps(sample_cookies()).encode())
    assert uid == '123456'
    assert len(cookies) == 3
    assert all(cookie['domain'] == '.facebook.com' for cookie in cookies)
    assert 'storeId' not in cookies[0]


def test_bad_cookie_payloads_are_rejected():
    cases = [b'', b'{invalid', json.dumps({'cookies': []}).encode(), b'a' * 262145]
    for payload in cases:
        with pytest.raises(InvalidCookies):
            parse_cookie_file(payload)
    invalid=sample_cookies()
    invalid['cookies'].append({'name':'s','value':'abc','domain':'facebook.com.evil.com'})
    with pytest.raises(InvalidCookies):
        parse_cookie_file(json.dumps(invalid).encode())
    invalid=sample_cookies()
    invalid['cookies']=[c for c in invalid['cookies'] if c['name']!='xs']
    with pytest.raises(InvalidCookies):
        parse_cookie_file(json.dumps(invalid).encode())


def test_real_user_provided_format_structure_compatibility():
    # Browser extension format: top-level {'url': ..., 'cookies': [records]}
    document=sample_cookies()
    document['cookies'][0].update({'storeId':'0','hostOnly':False,'session':True})
    cookies, uid=parse_cookie_file(json.dumps(document).encode())
    assert uid and len(cookies)==3
    assert not any('storeId' in c or 'hostOnly' in c for c in cookies)


def test_cookie_endpoint_import_and_update(client, monkeypatch):
    import fb_browser
    observed=[]

    async def pretend_import(account_id, cookies):
        observed.append((account_id, len(cookies)))

    monkeypatch.setattr(fb_browser, 'import_cookie_session', pretend_import)
    pipeline_id=client.post('/api/pipelines',json={'name':'@hiu9h'}).json()['id']
    account_id=client.post('/api/accounts',json={'pipeline_id':pipeline_id,'label':'Profile 1'}).json()['id']
    data={'file':('profile.json',json.dumps(sample_cookies()).encode(),'application/json')}
    response=client.post(f'/api/accounts/{account_id}/cookies',files=data)
    assert response.status_code==200,response.text
    assert response.json()['verified'] is False
    assert observed==[(account_id,3)]
    accounts=client.get('/api/accounts').json()
    assert accounts[0]['auth_method']=='cookie_file'
    assert accounts[0]['cookie_updated_at']
    assert 'fb_uid_fingerprint' not in accounts[0]
    assert 'xs' not in response.text
    repeat=client.post(f'/api/accounts/{account_id}/cookies',files=data)
    assert repeat.status_code==200
    assert len(observed)==2
    wrong=json.dumps(sample_cookies(uid='567890')).encode()
    mismatch=client.post(f'/api/accounts/{account_id}/cookies',files={'file':('other.json',wrong,'application/json')})
    assert mismatch.status_code==409
    assert len(observed)==2
    assert client.post('/api/accounts/12345/cookies',files=data).status_code==404


def test_bad_cookie_endpoint_does_not_modify_account(client, monkeypatch):
    import fb_browser
    called=[]

    async def pretend_import(account_id, cookies):
        called.append(True)

    monkeypatch.setattr(fb_browser, 'import_cookie_session', pretend_import)
    pipeline_id=client.post('/api/pipelines',json={'name':'@other'}).json()['id']
    account_id=client.post('/api/accounts',json={'pipeline_id':pipeline_id,'label':'Profile 2'}).json()['id']
    result=client.post(f'/api/accounts/{account_id}/cookies',files={'file':('bad.json',b'{bad','application/json')})
    assert result.status_code==422
    assert called==[]
    account=client.get('/api/accounts').json()[0]
    assert account['auth_method']=='browser'
    assert account['cookie_updated_at'] is None
