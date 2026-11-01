import assert from 'node:assert/strict';
import {test} from 'node:test';
import {transcriptLabel} from '../src/transcripts.ts';

test('正常定稿与停止、重启残稿及识别失败明确区分',()=>{
  assert.match(transcriptLabel({seq:1,status:'done',is_final:true}),/本窗已结束/);
  for(const reason of ['停止','worker重启']){
    assert.match(transcriptLabel({seq:1,status:'cancelled',is_final:true,error:reason}),/已中断/);
  }
  assert.match(transcriptLabel({seq:1,status:'error',is_final:true}),/识别失败/);
  assert.match(transcriptLabel({seq:1,status:'transcribing',is_final:false}),/持续修订/);
});
