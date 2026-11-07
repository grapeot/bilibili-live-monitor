import {useEffect,useState} from 'react';

export function useArchiveData<T>(url:string,interval:number,initial:T|null=null,validate?:(value:unknown)=>boolean){
 const [data,setData]=useState<T|null>(initial),[error,setError]=useState('');
 useEffect(()=>{
  let alive=true,busy=false,timer:ReturnType<typeof setTimeout>|undefined,controller:AbortController|undefined;
  const schedule=()=>{if(alive)timer=setTimeout(()=>void refresh(),interval)};
  async function refresh(){
   if(!alive||busy)return;
   clearTimeout(timer);
   if(document.hidden){schedule();return;}
   busy=true;controller=new AbortController();
   const timeout=setTimeout(()=>controller?.abort(),10000);
   try{
    const response=await fetch(url,{cache:'no-store',signal:controller.signal});
    if(!response.ok)throw Error('归档暂时无法读取');
    const value=await response.json();
    if(validate&&!validate(value))throw Error('归档暂时无法读取');
    if(alive){setData(value);setError('');}
   }catch{if(alive)setError('归档暂时无法读取');}
   finally{clearTimeout(timeout);busy=false;schedule();}
  }
  const visible=()=>{if(!document.hidden)void refresh()};
  document.addEventListener('visibilitychange',visible);
  void refresh();
  return()=>{alive=false;clearTimeout(timer);controller?.abort();document.removeEventListener('visibilitychange',visible)};
 },[url,interval,validate]);
 return {data,error};
}
