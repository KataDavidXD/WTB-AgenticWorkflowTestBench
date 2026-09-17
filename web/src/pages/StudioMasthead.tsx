import logo from '../../assets/WTB_Cyber_Black.png';
import { useStudio } from './StudioProvider';
import { projections } from './StudioTypes';
export function StudioMasthead() {
  const { state, dispatch, setModal, setDrawer, notify } = useStudio();
  const number = projections.find(p => p.id === state.view)!.number;
  const exportState = () => {
    const blob = new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = 'wtb-console-projection.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); notify('已导出当前真实数据投影。');
  };
  return <header className="masthead"><div className="brand"><img className="brand-logo" src={logo} alt="WTB CYBER logo" /><span className="brand-name">WTB</span><span className="brand-sub">WORKFLOW TEST BENCH<br /><span className="muted">CYBER · {number} DESIGN STUDY</span></span></div><span className="demo-label">本机 WTB · 真实 SDK 数据</span><div className="header-tools"><div className="theme-switcher" aria-label="视觉主题"><span className="theme-caption">THEME</span>{['GREY', 'JADE', 'CYBER'].map(theme => <a key={theme} className={`theme-link ${theme === 'CYBER' ? 'active' : ''}`} href="#cyber" aria-current={theme === 'CYBER' ? 'page' : undefined} onClick={e => { e.preventDefault(); if (theme !== 'CYBER') notify('当前实现仅提供 Cyber 主题。'); }}>{theme}</a>)}</div><button className="text-btn" id="guide-btn" onClick={() => setModal('guide')}>结构说明 <span className="mono">↗</span></button><button className="text-btn" id="export-btn" onClick={exportState}>导出状态</button><button className="text-btn" id="reset-btn" title="重新读取 WTB 当前状态" onClick={() => { setDrawer(false); setModal(null); dispatch({ type: 'reset' }); notify('已重新读取 WTB 当前状态。'); }}>刷新</button></div></header>;
}
