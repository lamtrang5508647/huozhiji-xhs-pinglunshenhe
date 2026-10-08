"""Owner-confirmed verification must not close or refresh the challenge."""
import sys
from types import SimpleNamespace
from xhs_visible_login import supported_url, verification_status, wait_for_owner_confirmation
import argparse

class LoginError(Exception):
    pass

sys.modules['xhs_cli.exceptions'] = SimpleNamespace(LoginError=LoginError)

class Page:
    context = SimpleNamespace(cookies=lambda: [{'name':'session','value':'fixture'}])
    def get_by_text(self, *args, **kwargs):
        return SimpleNamespace(count=lambda:0)

class Client:
    _page = Page()
    checks = 0
    def _raise_if_blocked(self, *args, **kwargs):
        self.checks += 1
        if self.checks == 1:
            raise LoginError('verification not completed')

confirmations=[]
saved=[]
client=Client()
wait_for_owner_confirmation(client, {'session'}, saved.append,
                            confirm=lambda: confirmations.append(True))
assert len(confirmations)==2 and saved==['session=fixture']
assert supported_url('https://www.xiaohongshu.com/explore/fixture?xsec_token=fixture')
for url in ('https://xiaohongshu.com.evil.example/a', 'file:///tmp/a', 'https://user:pass@xiaohongshu.com/a'):
    try:
        supported_url(url)
    except argparse.ArgumentTypeError:
        pass
    else:
        raise AssertionError('external URL accepted')
assert verification_status(client, LoginError('Blocked by security verification')) == 'captcha'
assert verification_status(client, LoginError('IP存在风险 300012')) == 'risk_control'
assert verification_status(client, LoginError('Blocked by security verification: IP存在风险')) == 'risk_control'
assert verification_status(client, LoginError('login required')) == 'session_expired'
print('verification window confirmation tests passed')
