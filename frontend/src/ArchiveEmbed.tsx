import React from 'react';
import {createRoot} from 'react-dom/client';
import {ArchivePage,type PublishedArchive,type ArchivePageSettings} from './ArchivePage';
const target=document.getElementById('archive-root');
if(target){
 const data=JSON.parse(document.getElementById('archive-data')!.textContent!) as PublishedArchive;
 const settings=JSON.parse(document.getElementById('archive-settings')!.textContent!) as ArchivePageSettings;
 createRoot(target).render(<ArchivePage initial={data} settings={settings}/>);
}
