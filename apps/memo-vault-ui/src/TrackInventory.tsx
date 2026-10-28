import { useEffect, useState } from 'react';
import { claudeRequest } from './api';

interface Inventory {
  batch_id: string; package_id: string; tracks: Record<string, number>;
  agent_events_stored: number; total: number; page: number; page_size: number;
  items: { record_id: string; record_type: string; track: string; status: string;
    target_id?: string | null; name?: string; source_member?: string }[];
  source_files: { name: string; bytes?: number; status: string; container?: string }[];
}
const labels: Record<string, string> = { conversation: '用户对话', agent: 'Agent 轨迹',
  knowledge: 'Knowledge 文件', assertion: '平台记忆声明', other: '其他 UMIF 记录' };

export default function TrackInventory({ batchId }: { batchId: string }) {
  const [page, setPage] = useState(1);
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    void claudeRequest<Inventory>(`imports/${batchId}/content-inventory?page=${page}&page_size=30`)
      .then(result => { if (current) { setInventory(result); setError(null); } })
      .catch(reason => { if (current) setError(String(reason)); });
    return () => { current = false; };
  }, [batchId, page]);
  return <section className="track-inventory"><h3>归档内容轨道</h3>
    {error && <p className="notice error">读取内容清单失败：{error}</p>}
    {!inventory && !error && <p>正在读取 UMIF 清单…</p>}
    {inventory && <><div className="track-counts">{Object.entries(inventory.tracks).map(([track, count]) =>
      <span key={track}><strong>{count}</strong> {labels[track] ?? track}</span>)}</div>
      <p className="subtle">UMIF 中有 {inventory.tracks.agent ?? 0} 条 Agent 轨迹；Vault 独立工具事件表保留 {inventory.agent_events_stored} 条。工具参数和结果不进入用户记忆抽取。</p>
      <details><summary>来源文件与 ZIP 内成员（{inventory.source_files.length}）</summary><ul>
        {inventory.source_files.map((file, index) => <li key={`${file.container ?? ''}:${file.name}:${index}`}>{file.container ? `${file.container} / ` : ''}{file.name} · {file.status}{file.bytes !== undefined ? ` · ${file.bytes} B` : ''}</li>)}
      </ul></details>
      <details><summary>UMIF 记录（{inventory.total}）</summary><div className="track-record-list">{inventory.items.map(item => <div key={item.record_id}>
        <strong>{labels[item.track] ?? item.track}</strong><span>{item.name ?? item.record_type} · {item.status} · {item.source_member ?? item.record_id}</span>
      </div>)}</div><div className="actions"><button className="secondary-button" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><button className="secondary-button" disabled={page * inventory.page_size >= inventory.total} onClick={() => setPage(page + 1)}>下一页</button></div></details>
    </>}
  </section>;
}
