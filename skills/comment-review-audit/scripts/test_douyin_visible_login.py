#!/usr/bin/env python3
from pathlib import Path
from unittest.mock import Mock, patch
import douyin_visible_login as login

def run():
    page=Mock()
    context=Mock()
    with patch.object(login,'security_status',side_effect=['captcha','']), patch('builtins.input',side_effect=['','']) as confirm:
        login.wait_for_owner_confirmation(page,context,Path('/tmp/audit-test-profile'))
    assert confirm.call_count==2
    context.storage_state.assert_called_once()
    context.close.assert_not_called()
    page.goto.assert_not_called()
    print('douyin verification confirmation tests passed')

if __name__=='__main__': run()
