import React, {createContext, useContext} from 'react';

export type Instance = {id:string;label:string;mode?:string;base:string};
const defaultInstance:Instance={id:'local',label:'',base:import.meta.env.BASE_URL};
const Context=createContext(defaultInstance);
export const InstanceProvider=({instance,children}:{instance:Instance;children:React.ReactNode})=><Context.Provider value={instance}>{children}</Context.Provider>;
export const useInstance=()=>useContext(Context);
export function useApi(){
  const {base}=useInstance();
  return async(path:string,init?:RequestInit)=>{
    const response=await fetch(base+'api/'+path,init);
    const result=await response.json();
    if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));
    return result;
  };
}
