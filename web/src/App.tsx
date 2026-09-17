import { BranchAtlasPage } from './pages/BranchAtlasPage';
import { ExecutionSectionPage } from './pages/ExecutionSectionPage';
import { PathLoomPage } from './pages/PathLoomPage';
import { VariantAtlasPage } from './pages/VariantAtlasPage';
import { StudioPageFrame } from './pages/StudioPageFrame';
import { StudioProvider, useStudio } from './pages/StudioProvider';
import './pages/structural-studio.css';

function Studio() {
  const { state } = useStudio();
  const pages = { loom: <PathLoomPage />, lineage: <BranchAtlasPage />, section: <ExecutionSectionPage />, atlas: <VariantAtlasPage /> };
  return <StudioPageFrame>{pages[state.view]}</StudioPageFrame>;
}
export default function App() { return <StudioProvider><Studio /></StudioProvider>; }
