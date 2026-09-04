import React, { useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, RefreshCw, ShieldCheck } from 'lucide-react';
import { get, post } from '../lib/request';

const RELEASES = 'https://github.com/genoooool/xianyu-super-butler/releases';
interface UpdateStatus {
  available?: boolean; phase: string; version: string; latest_version: string;
  notes: string; error: string; progress: number;
}
const labels: Record<string, string> = {
  idle: '点击检查，查看是否有新版本', checking: '正在检查更新…',
  available: '发现新版本', current: '当前已是最新正式版本',
  unpublished: '尚未发布更新清单', incompatible: '此版本暂不支持应用内安装',
  downloading: '正在下载并校验安装包…', installing: '正在安装，完成后自动重新打开…',
  error: '更新未完成',
};

export default function SoftwareUpdate() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const busyRef = useRef(false);
  const phaseRef = useRef('idle');
  const generation = useRef(0);
  const mounted = useRef(true);
  const isBusy = submitting || ['checking', 'downloading', 'installing'].includes(status?.phase || '');

  useEffect(() => {
    mounted.current = true;
    let active = true;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || busyRef.current) return;
      inFlight = true;
      const current = generation.current;
      try {
        const value = await get<UpdateStatus>('/desktop/updates/status');
        if (active && !busyRef.current && current === generation.current) {
          setStatus(value); phaseRef.current = value.phase; setError('');
        }
      } catch {
        if (active && phaseRef.current !== 'installing') setError('无法读取更新状态，请检查登录或稍后重试');
      } finally { inFlight = false; }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 1500);
    return () => { active = false; mounted.current = false; window.clearInterval(timer); };
  }, []);

  const act = async (action: 'check' | 'install') => {
    if (busyRef.current || isBusy) return;
    busyRef.current = true; setSubmitting(true); setConfirm(false); setError('');
    generation.current += 1;
    try {
      const value = await post<UpdateStatus>('/desktop/updates/action', { action, version: action === 'install' ? status?.latest_version : '' });
      if (mounted.current) {
        setStatus(previous => ({ ...value, available: previous?.available })); phaseRef.current = value.phase;
      }
    } catch (err: any) {
      if (mounted.current) setError(typeof err.response?.data?.detail === 'string' ? err.response.data.detail : '请求未确认，请查看状态后再操作');
    } finally {
      busyRef.current = false;
      if (mounted.current) setSubmitting(false);
    }
  };

  return (
    <section className="section-panel p-5 sm:p-6" aria-label="软件更新">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold text-gray-900">软件更新</h2>
          <p className="mt-2 text-sm text-gray-500">当前版本 <span className="font-semibold text-gray-900">{status?.version || '—'}</span></p>
        </div>
        <button type="button" onClick={() => void act('check')} disabled={isBusy || !status?.available}
          className="ios-btn-primary flex items-center gap-2 rounded-full px-5 py-3 disabled:opacity-50">
          <RefreshCw className={`h-4 w-4 ${status?.phase === 'checking' ? 'animate-spin' : ''}`} />检查更新
        </button>
      </div>
      <div className="mt-6 border-t border-gray-200 pt-5" role="status">
        <p className="font-semibold text-gray-900">{labels[status?.phase || 'idle'] || '读取更新状态中…'}</p>
        {status?.latest_version && <p className="mt-2 text-sm text-gray-500">发布版本 {status.latest_version}</p>}
        {status?.notes && <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-gray-600">{status.notes}</p>}
        {status?.phase === 'downloading' && <div className="mt-4">
          <progress className="h-2 w-full accent-[#e6c900]" max={100} value={status.progress || undefined} aria-label="下载进度" />
          <p className="mt-1 text-xs text-gray-500">{status.progress ? `${status.progress}%` : '正在连接下载服务…'}</p>
        </div>}
      </div>
      {(error || status?.error) && <p role="alert" className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{error || status?.error}</p>}
      {status?.available === false && <p className="mt-4 text-sm text-gray-500">应用内升级仅支持 macOS 桌面版；其他环境请从 Releases 下载。</p>}
      {status?.phase === 'available' && <button type="button" disabled={isBusy} onClick={() => setConfirm(true)}
        className="ios-btn-primary mt-5 inline-flex items-center gap-2 rounded-full px-5 py-3"><Download className="h-4 w-4" />下载并升级</button>}
      <div className="mt-6 flex gap-2 text-xs leading-6 text-gray-500">
        <ShieldCheck className="mt-1 h-4 w-4 shrink-0" />
        <p>只安装本工作台签名的正式版本。升级替换程序，不清理账号、QA、图片或订单目录；正在处理业务时暂不安装。</p>
      </div>
      <a className="mt-3 inline-flex items-center gap-1 text-sm text-gray-600 underline underline-offset-4" href={RELEASES} target="_blank" rel="noopener noreferrer">
        从 GitHub Releases 手动下载<ExternalLink className="h-3 w-3" />
      </a>
      {confirm && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={() => setConfirm(false)}>
        <div role="dialog" aria-modal="true" aria-labelledby="update-confirm-title" className="section-panel w-full max-w-md p-6" onClick={e => e.stopPropagation()}>
          <h3 id="update-confirm-title" className="text-xl font-bold text-gray-900">升级到 {status?.latest_version}？</h3>
          <p className="mt-3 text-sm leading-7 text-gray-600">下载并校验后将短暂退出并重新打开工作台。请先保存正在编辑的内容；原有业务数据保留。</p>
          <div className="mt-6 flex justify-end gap-3">
            <button type="button" className="ios-btn-secondary rounded-full px-5 py-2" onClick={() => setConfirm(false)}>取消</button>
            <button type="button" className="ios-btn-primary rounded-full px-5 py-2" onClick={() => void act('install')}>确认升级</button>
          </div>
        </div>
      </div>}
    </section>
  );
}
