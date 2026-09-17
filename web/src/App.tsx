import { BranchAtlasPage } from './pages/BranchAtlasPage';
import { ExecutionSectionPage } from './pages/ExecutionSectionPage';
import { PathLoomPage } from './pages/PathLoomPage';
import { VariantAtlasPage } from './pages/VariantAtlasPage';
import { StudioPageFrame } from './pages/StudioPageFrame';
import { StudioProvider, useStudio } from './pages/StudioProvider';
import './pages/structural-studio.css';

function Studio() {
  const { state, ready, message } = useStudio();
  if (!ready || !state.runs.length) return <main className="work-surface"><div className="stage"><p className="muted">{message || (ready ? '尚无真实执行。请通过 WTB API 或当前项目启动一次执行。' : '正在读取本机 WTB 服务…')}</p></div></main>;
  const pages = { loom: <PathLoomPage />, lineage: <BranchAtlasPage />, section: <ExecutionSectionPage />, atlas: <VariantAtlasPage /> };
  return <StudioPageFrame>{pages[state.view]}</StudioPageFrame>;
}
export default function App() { return <StudioProvider><Studio /></StudioProvider>; }
