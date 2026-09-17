import { PathLoomStage } from './PathLoomStage';
import { PathLoomToolbar } from './StudioToolbars';
import { StudioView } from './StudioView';

export function PathLoomPage() { return <StudioView toolbar={<PathLoomToolbar />}><PathLoomStage /></StudioView>; }
