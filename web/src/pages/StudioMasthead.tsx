import logo from '../../assets/WTB_Cyber_Black.png';
import { variants } from './StudioDemoData';
import { useStudio } from './StudioProvider';
import { projections } from './StudioTypes';
export function StudioMasthead() {
  const { state, dispatch, setModal, setDrawer, setAuto, notify } = useStudio();
  const number = projections.find(p => p.id === state.view)!.number;
  const exportState = () => {
    const blob = new Blob([JSON.stringify({ demo: true, note: 'Simulation data; not a production WTB API schema.', variants, ...state }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = 'wtb-structural-demo-state.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); notify('已导出当前示例定义、运行、检查点与历史。');
  };
  return <header className="masthead"><div className="brand"><img className="brand-logo" src={logo} alt="WTB CYBER logo" /><span className="brand-name">WTB</span><span className="brand-sub">WORKFLOW TEST BENCH<br /><span className="muted">CYBER · {number} DESIGN STUDY</span></span></div><span className="demo-label">示例数据 · 操作仅发生在浏览器，不连接 WTB / Ray</span><div className="header-tools"><div className="theme-switcher" aria-label="视觉主题"><span className="theme-caption">THEME</span>{['GREY', 'JADE', 'CYBER'].map(theme => <a key={theme} className={`theme-link ${theme === 'CYBER' ? 'active' : ''}`} href="#cyber" aria-current={theme === 'CYBER' ? 'page' : undefined} onClick={e => { e.preventDefault(); if (theme !== 'CYBER') notify('本次还原保持 Cyber 主题。'); }}>{theme}</a>)}</div><button className="text-btn" id="guide-btn" onClick={() => setModal('guide')}>结构说明 <span className="mono">↗</span></button><button className="text-btn" id="export-btn" onClick={exportState}>导出状态</button><button className="text-btn" id="reset-btn" title="清除演示操作并恢复初始数据" onClick={() => { setAuto(false); setDrawer(false); setModal(null); dispatch({ type: 'reset' }); notify('示例已恢复；四个视角重新共享初始状态。'); }}>重置</button></div></header>;
}
