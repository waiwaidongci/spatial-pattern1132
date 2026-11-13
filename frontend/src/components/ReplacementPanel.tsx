import {useMemo,useState} from 'react';
import {Upload,CheckCircle2,ClipboardCheck} from 'lucide-react';
import {Cell,Edge} from '../types/domain';
import {confirmReplacement,parseReplacementCells,parseReplacementEdges,preflightReplacement,PreflightResult} from '../api/replacements';
import {Notice} from './common/Notice';
import {DataTable} from './common/DataTable';

interface Props{surveyId:number;surveyCode:string;cells:Cell[];edges:Edge[];canWrite:boolean;onConfirmed:()=>Promise<void>}
type Stage='idle'|'preflighted'|'confirmed';

const cellsToCsv=(cells:{code:string;row:number;col:number;value:number;note?:string}[])=>
  cells.map(c=>[c.code,c.row,c.col,c.value,(c.note??'').replace(/\r?\n/g,' ')].join(',')).join('\n');
const edgesToCsv=(edges:{a:string;b:string}[])=>edges.map(e=>`${e.a},${e.b}`).join('\n');

export function ReplacementPanel({surveyId,surveyCode,cells,edges,canWrite,onConfirmed}:Props){
  const [open,setOpen]=useState(false);
  const [cellCsv,setCellCsv]=useState('');
  const [edgeCsv,setEdgeCsv]=useState('');
  const [result,setResult]=useState<PreflightResult|null>(null);
  const [tokenEcho,setTokenEcho]=useState('');
  const [acked,setAcked]=useState(false);
  const [stage,setStage]=useState<Stage>('idle');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [note,setNote]=useState('');

  const parsedCells=useMemo(()=>cellCsv.trim()?parseReplacementCells(cellCsv):null,[cellCsv]);
  const parsedEdges=useMemo(()=>parseReplacementEdges(edgeCsv),[edgeCsv]);
  const localErrors=[...(parsedCells?.errors??[]),...(parsedEdges?.errors??[])];
  const sameAsLive=result?.status==='ready'&&result.preserved_notes===cells.length&&!result.added_codes?.length&&!result.removed_codes?.length;

  function reset(){setResult(null);setTokenEcho('');setAcked(false);setStage('idle');setError('');setNote('')}
  function loadFile(file:File|undefined,setter:(v:string)=>void){
    reset();
    if(!file){setter('');return}
    const reader=new FileReader();
    reader.onload=()=>setter(String(reader.result??''));
    reader.onerror=()=>setError('无法读取该文件');
    reader.readAsText(file,'UTF-8');
  }
  function loadCurrent(){reset();setCellCsv(cellsToCsv(cells.map(c=>({code:c.code,row:c.row,col:c.col,value:c.value,note:c.note}))));setEdgeCsv(edgesToCsv(edges));}

  async function preflight(){
    if(localErrors.length){setError('本地解析未通过，先修正：'+localErrors.slice(0,5).join('；'));return}
    const cellsPayload=parsedCells?.cells??[];
    if(!cellsPayload.length){setError('至少需要一行格点');return}
    setBusy(true);setError('');setTokenEcho('');setAcked(false);
    try{
      const res=await preflightReplacement(surveyId,{cells:cellsPayload,edges:parsedEdges?.edges??[]});
      setResult(res);setStage('preflighted');
    }catch(e){setError((e as Error).message)}
    finally{setBusy(false)}
  }
  async function confirm(){
    if(!result?.token||!acked||tokenEcho.trim()!==result.token){setError('回填令牌并勾选确认后才能提交');return}
    setBusy(true);setError('');
    try{
      const done=await confirmReplacement(surveyId,result.token);
      setStage('confirmed');
      setNote(`批次 #${done.replacement_id} 已确认；${done.cell_count} 格 / ${done.edge_count} 边，保留批注 ${done.preserved_notes}。新研究将读取这批数据。`);
      await onConfirmed();
    }catch(e){setError((e as Error).message)}
    finally{setBusy(false)}
  }

  if(!open)return <div className="replacement-entry">
    <div><span className="eyebrow">WHOLE BATCH REPLACEMENT</span><h2>整期替换网格与邻接</h2><p>只核对不能补正时，先预检再确认：按格编号保留批注，检查数值、位置与无向邻接，坏行集中说明且整批不落库。确认只接受本次预检的原批次；期间来源、数值、批注或边有改动就拒绝覆盖。既有研究快照与结果不动，只有确认后的新研究读取新数据。</p></div>
    <button className="outline" disabled={!canWrite} onClick={()=>setOpen(true)}><Upload size={14}/>{canWrite?'开始整期替换':'阅读者只读'}</button>
  </div>;

  return <section className="replacement-panel">
    <div className="section-top"><div><span className="eyebrow">WHOLE BATCH REPLACEMENT / {surveyCode}</span><h2>整期替换：预检 → 确认</h2></div><button className="text-link" onClick={()=>{reset();setOpen(false)}}>收起</button></div>
    <p className="caption">预检只读：数值须有限且|值|≤1e9；位置为唯一非负行/列；无向邻接拒绝自环、未知端点与正反向重复（a↔b 与 b↔a 同一条）。任一坏行会集中列出并整批不落库。预检通过才返回一次性令牌；确认须回填令牌并显式勾选。批注按格编号保留，未提交的旧编号视为该格移除。当前期 {cells.length} 格 / {edges.length} 无向边。{result?.rule?` ${result.rule}`:''}</p>
    <Notice message={error}/>
    <div className="replacement-grid">
      <div>
        <div className="replacement-actions"><button className="outline" disabled={busy} onClick={loadCurrent}>载入当前期</button><span className="caption">或从 CSV 导入</span></div>
        <label>格点 CSV（code,row,col,value[,note]）<input aria-label="格点CSV文件" type="file" accept=".csv,text/csv" disabled={busy} onChange={e=>loadFile(e.target.files?.[0],setCellCsv)}/></label>
        <label>格点内容<textarea aria-label="格点CSV内容" value={cellCsv} rows={10} disabled={busy} onChange={e=>{reset();setCellCsv(e.target.value)}} placeholder="code,row,col,value,note"/></label>
        <label>邻接 CSV（a,b，每条无向边一行；正反向重复会被预检拒绝）<input aria-label="邻接CSV文件" type="file" accept=".csv,text/csv" disabled={busy} onChange={e=>loadFile(e.target.files?.[0],setEdgeCsv)}/></label>
        <label>邻接内容<textarea aria-label="邻接CSV内容" value={edgeCsv} rows={7} disabled={busy} onChange={e=>{reset();setEdgeCsv(e.target.value)}} placeholder="a,b"/></label>
        {localErrors.length>0&&<div className="replacement-broken" aria-live="polite"><b>本地解析提示（{localErrors.length}）</b><ul>{localErrors.slice(0,8).map((x,i)=><li key={i}>{x}</li>)}</ul><small>以服务端预检的坏行清单为准；本地只挡住格式问题。</small></div>}
        <button className="primary" disabled={busy||!canWrite} onClick={preflight}><ClipboardCheck size={14}/>预检整批</button>
      </div>
      <aside aria-live="polite">
        {stage==='confirmed'&&note?<div className="replacement-verdict ready"><b>已确认整批写入</b><small>{note}</small></div>:null}
        {!result&&<p className="role-note">预检不写库。确认只在预检通过、且令牌仍对应原批次时可用。</p>}
        {result&&<>
          <div className={'preflight-verdict '+result.status}><b>{result.status==='ready'?'预检通过：坏行 0，整批待确认':'预检未通过：整批不落库'}</b><small>{result.summary.cell_rows} 行格点 / {result.summary.edge_rows} 行边 / 坏行 {result.summary.broken}</small></div>
          {result.status==='ready'&&<>
            <dl className="preflight-meta">
              <div><dt>格点</dt><dd>{result.cell_count}</dd></div>
              <div><dt>无向边</dt><dd>{result.edge_count}</dd></div>
              <div><dt>按编号保留批注</dt><dd>{result.preserved_notes}</dd></div>
              <div><dt>新增编号</dt><dd>{result.added_codes?.length??0}</dd></div>
              <div><dt>移除编号</dt><dd>{result.removed_codes?.length??0}</dd></div>
              <div><dt>度数范围</dt><dd>{result.graph?`${result.graph.min_degree}–${result.graph.max_degree}`:'—'}</dd></div>
            </dl>
            {sameAsLive&&<p className="caption warning">清单与当前期一致；替换不会改变已确认数据，如需重跑请只建立新研究。</p>}
            {result.removed_codes?.length?<p className="caption warning">将移除编号：{result.removed_codes.join('、')}</p>:null}
            {result.runs_on_period?.length?<p className="caption">该期已有 {result.runs_on_period.length} 个研究（#{result.runs_on_period.map(r=>r.run_id).join(', #')}）；其快照与结果保持原样。</p>:null}
            <div className="confirm-box">
              <label>回填批次令牌<input aria-label="确认令牌" value={tokenEcho} disabled={busy||stage==='confirmed'} onChange={e=>setTokenEcho(e.target.value.trim())} placeholder={result.batch_hash?.slice(0,16)}/></label>
              <label className="checkbox"><input type="checkbox" aria-label="确认覆盖整期数据" checked={acked} disabled={busy||stage==='confirmed'} onChange={e=>setAcked(e.target.checked)}/>我确认这是预检过的原批次，整期覆盖不可逆</label>
              <small>令牌只对这次预检有效。预检后来源数值、位置、批注或邻接边有改动，确认会被拒绝，整批不落库。</small>
              <button className="primary" disabled={busy||!canWrite||stage==='confirmed'||!acked||tokenEcho!==result.token} onClick={confirm}><CheckCircle2 size={14}/>确认整期替换</button>
            </div>
          </>}
          {result.status==='invalid'&&<div className="replacement-broken"><b>坏行清单（{result.broken_rows.length}）— 整批不落库</b>
            <DataTable head={['行','位置标识','格点','字段','问题']}>{result.broken_rows.map((b,i)=><tr key={i}><td>{b.row??'—'}</td><td>{b.label}</td><td>{b.code??'—'}</td><td>{b.field}</td><td>{b.message}</td></tr>)}</DataTable>
            <small>修好后再提交预检；不会部分应用。</small></div>}
        </>}
      </aside>
    </div>
  </section>;
}
