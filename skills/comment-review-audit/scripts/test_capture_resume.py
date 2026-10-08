#!/usr/bin/env python3
from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from capture_resume import reusable_capture, pending_targets, PARSER_REVISIONS

def main():
    now = datetime.now(timezone.utc)
    good = {'platform':'douyin','target_key':'a','capture_status':'ok',
            'capture_complete':True,'image_detection_complete':True,
            'comments':[{'text':'正文','has_image':False}],
            'captured_at':now.isoformat(),'parser_revision':PARSER_REVISIONS['douyin']}
    assert reusable_capture(good,'douyin','a',now=now)
    for change in ({'capture_complete':False},{'capture_status':'captcha'},
                   {'image_detection_complete':False},{'parser_revision':'old'},
                   {'captured_at':(now-timedelta(hours=25)).isoformat()},
                   {'captured_at':(now+timedelta(hours=1)).isoformat()},
                   {'captured_at':'bad'},{'comments':[{'text':'正文'}]},
                   {'platform':'xiaohongshu'}):
        assert not reusable_capture({**good,**change},'douyin','a',now=now),change
    targets=[{'target_key':'a'},{'target_key':'b'}]
    assert pending_targets(targets,{'a':good},'douyin','target_key') == targets[1:]
    assert pending_targets(targets,{'a':good},'douyin','target_key',refresh=True) == targets
    assert pending_targets(targets[:1],{'a':good},'douyin','target_key') == []
    # Real entry points must return before creating a browser when nothing remains.
    import douyin_batch_capture as dy
    import xhs_batch_capture as xhs
    with tempfile.TemporaryDirectory() as temp:
        checkpoint=Path(temp)/'observations.json'
        checkpoint.write_text(json.dumps({'observations':[good]}))
        with patch.object(dy,'targets_from_expected',return_value=targets[:1]), \
             patch.object(dy,'sync_playwright',side_effect=AssertionError('browser should not open')), \
             patch('sys.argv',['capture','--expected','fixture.json','--output',str(checkpoint)]):
            assert dy.main()==0
        xhs_good={**good,'platform':'xiaohongshu','parser_revision':PARSER_REVISIONS['xiaohongshu']}
        checkpoint.write_text(json.dumps({'observations':[xhs_good]}))
        with patch.object(xhs,'targets_from_workbooks',return_value=[{'note_id':'a'}]), \
             patch.object(xhs,'load_xhs_modules',side_effect=AssertionError('browser should not open')):
            assert xhs.main(['--input','fixture.xlsx','--output',str(checkpoint)])==0
    print('capture resume tests passed')

if __name__=='__main__': main()
