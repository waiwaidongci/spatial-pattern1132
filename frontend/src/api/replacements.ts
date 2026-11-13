import {request} from './client';

export interface ReplacementCell {code:string;row:number;col:number;value:number;note?:string}
export interface ReplacementEdge {a:string;b:string}
export interface BrokenRow {row:number|null;label:string;code:string|null;field:string;message:string;value:unknown}
export interface PreflightResult {
  status:'ready'|'invalid';survey_id:number;survey_code:string;
  summary:{cell_rows:number;edge_rows:number;broken:number;kept_notes_rule:string};
  broken_rows:BrokenRow[];
  token?:string|null;replacement_id?:number;batch_hash?:string;source_state_hash?:string;
  cell_count?:number;edge_count?:number;preserved_notes?:number;removed_codes?:string[];added_codes?:string[];
  graph?:{n:number;edges:number;S0:number;min_degree:number;max_degree:number;weight_rule:string};
  rule?:string;runs_on_period?:{run_id:number;name:string;status:string;created_at:string}[];
}
export interface ReplacementSummary {
  replacement_id:number;survey_id:number;status:string;batch_hash:string;source_state_hash:string;
  cell_count:number;edge_count:number;preserved_notes:number;submitted_by:string;created_at:string;confirmed_by:string;confirmed_at:string;
}

export const preflightReplacement=(id:number,data:{cells:ReplacementCell[];edges:ReplacementEdge[]})=>
  request<PreflightResult>(`/surveys/${id}/replacement/preflight`,{method:'POST',body:JSON.stringify(data)});
export const confirmReplacement=(id:number,token:string)=>
  request<ReplacementSummary>(`/surveys/${id}/replacement/confirm`,{method:'POST',body:JSON.stringify({token,confirm:true})});
export const getReplacements=(id:number)=>request<ReplacementSummary[]>(`/surveys/${id}/replacement`);

const linesOf=(text:string)=>text.replace(/^﻿/,'').split(/\r?\n/).filter(line=>line.trim().length);
const splitCsvLine=(line:string):string[]=>{
  const out:string[]=[];let cur='';let quoted=false;
  for(let i=0;i<line.length;i++){const ch=line[i];
    if(ch==='"'){if(quoted&&line[i+1]==='"'){cur+='"';i++}else quoted=!quoted}
    else if(ch===','&&!quoted){out.push(cur);cur=''} else cur+=ch}
  out.push(cur);return out;
};
const integerText=(s:string)=>/^\d+$/.test(s);
const numericText=(s:string)=>s!==''&&Number.isFinite(Number(s));

/** Parse the cell sheet: code,row,col,value[,note]; header aliases cover common instrument spellings. */
export const parseReplacementCells=(text:string):{cells:ReplacementCell[];errors:string[]}=>{
  const lines=linesOf(text);const cells:ReplacementCell[]=[];const errors:string[]=[];
  if(!lines.length)return {cells,errors:['格点 CSV 为空']};
  const header=splitCsvLine(lines[0]).map(h=>h.trim().toLowerCase());
  const aliases:Record<string,string[]>={code:['code','格点','格点编号'],row:['row','行'],col:['col','列'],value:['value','均值','dba','均值dba'],note:['note','批注']};
  const at=(field:string)=>header.findIndex(h=>aliases[field].includes(h));
  const missing=['code','row','col','value'].filter(f=>at(f)<0);
  if(missing.length)return {cells,errors:[`表头缺少必需列：${missing.join('、')}；实际表头为「${lines[0]}」`]};
  lines.slice(1).forEach((line,i)=>{
    const parts=splitCsvLine(line);const rowNum=i+2;
    const code=(parts[at('code')]??'').trim(),row=(parts[at('row')]??'').trim(),col=(parts[at('col')]??'').trim(),value=(parts[at('value')]??'').trim();
    if(!code){errors.push(`第${rowNum}行：缺少格点编号`);return}
    if(!integerText(row)||!integerText(col)){errors.push(`第${rowNum}行（${code}）：行/列须为非负整数`);return}
    if(!numericText(value)){errors.push(`第${rowNum}行（${code}）：均值须为数值`);return}
    cells.push({code,row:Number(row),col:Number(col),value:Number(value),note:at('note')>=0?(parts[at('note')]??''):undefined});
  });
  return {cells,errors};
};

/** Parse the edge sheet: a,b. Reversed orientation is the same undirected edge; the server dedupes. */
export const parseReplacementEdges=(text:string):{edges:ReplacementEdge[];errors:string[]}=>{
  if(!text.trim())return {edges:[],errors:[]};
  const lines=linesOf(text);const edges:ReplacementEdge[]=[];const errors:string[]=[];
  const header=splitCsvLine(lines[0]).map(h=>h.trim().toLowerCase());
  const at=(names:string[])=>header.findIndex(h=>names.includes(h));
  const aAt=at(['a','端点a']),bAt=at(['b','端点b']);
  const start=aAt>=0&&bAt>=0?1:0;
  if(start===0&&(aAt>=0||bAt>=0))return {edges,errors:['邻接 CSV 表头须同时含 a 和 b']};
  lines.slice(start).forEach((line,i)=>{
    const parts=splitCsvLine(line);const rowNum=i+start+1;
    const a=(parts[aAt>=0?aAt:0]??'').trim(),b=(parts[bAt>=0?bAt:1]??'').trim();
    if(!a||!b){errors.push(`第${rowNum}行：端点a和b不能为空`);return}
    edges.push({a,b});
  });
  return {edges,errors};
};
