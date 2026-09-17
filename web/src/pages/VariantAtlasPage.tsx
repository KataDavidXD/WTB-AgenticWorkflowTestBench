import { VariantAtlasStage } from './VariantAtlasStage';
import { VariantAtlasToolbar } from './StudioToolbars';
import { StudioView } from './StudioView';

export function VariantAtlasPage() { return <StudioView toolbar={<VariantAtlasToolbar />}><VariantAtlasStage /></StudioView>; }
