import type { DocumentStatus } from '../types';
import { statusBadgeClass, statusLabel } from '../status';

/**
 * Renders the exact status the API reports (title-cased; CSS uppercases it for display).
 * The class mapping lives in `../status` so it is unit-testable for every status.
 */
export function StatusBadge({ status }: { status: DocumentStatus }): JSX.Element {
  return <span className={statusBadgeClass(status)}>{statusLabel(status)}</span>;
}
