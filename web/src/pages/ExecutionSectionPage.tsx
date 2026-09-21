import { ExecutionSectionStage } from "./ExecutionSectionStage";
import { ExecutionSectionToolbar } from "./StudioToolbars";
import { StudioView } from "./StudioView";

export function ExecutionSectionPage() {
  return (
    <StudioView toolbar={<ExecutionSectionToolbar />}>
      <ExecutionSectionStage />
    </StudioView>
  );
}
