import { BranchAtlasPage } from "./pages/BranchAtlasPage";
import { ExecutionSectionPage } from "./pages/ExecutionSectionPage";
import { PathLoomPage } from "./pages/PathLoomPage";
import { VariantAtlasPage } from "./pages/VariantAtlasPage";
import { StudioPageFrame } from "./pages/StudioPageFrame";
import { StudioProvider, useStudio } from "./pages/StudioProvider";
import { ConsoleWorkspace } from "./pages/ConsoleWorkspace";
import "./pages/structural-studio.css";

function Studio() {
  const { state, ready, message, catalog } = useStudio();
  if (!ready || !catalog?.variants.length)
    return (
      <main className="work-surface">
        <div className="stage">
          <p className="muted">
            {message ||
              (ready
                ? "尚无注册项目。请在服务配置中注册 WorkflowProject 工厂。"
                : "正在读取本机 WTB 服务…")}
          </p>
        </div>
      </main>
    );
  const pages = {
    loom: <PathLoomPage />,
    lineage: <BranchAtlasPage />,
    section: <ExecutionSectionPage />,
    atlas: <VariantAtlasPage />,
  };
  return <StudioPageFrame>{pages[state.view]}</StudioPageFrame>;
}
export default function App() {
  return (
    <StudioProvider>
      <ConsoleWorkspace>
        <Studio />
      </ConsoleWorkspace>
    </StudioProvider>
  );
}
