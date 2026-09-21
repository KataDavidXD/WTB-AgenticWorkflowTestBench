import { BranchAtlasStage } from "./BranchAtlasStage";
import { BranchAtlasToolbar } from "./StudioToolbars";
import { StudioView } from "./StudioView";

export function BranchAtlasPage() {
  return (
    <StudioView toolbar={<BranchAtlasToolbar />}>
      <BranchAtlasStage />
    </StudioView>
  );
}
