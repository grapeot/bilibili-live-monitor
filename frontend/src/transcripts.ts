export function transcriptLabel(item:{seq?:number|null;status?:string|null;is_final?:boolean|null}){
  const state=item.status==='cancelled'?'已中断 · 保留草稿':
    item.status==='error'?'识别失败':item.is_final?'本窗已结束':'持续修订';
  return `实时稿 #${item.seq} · ${state}`;
}
