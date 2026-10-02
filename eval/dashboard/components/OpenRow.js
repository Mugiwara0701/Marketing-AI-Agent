'use client';
import { useDrawer } from './DrawerProvider';

// Table row that opens the shared drawer for one item.
export function OpenRow({ kind, id, children }) {
  const { open, isSelected } = useDrawer();
  return (
    <tr
      tabIndex={0}
      className={isSelected(kind, id) ? 'selected' : undefined}
      onClick={() => open(kind, id)}
      onKeyDown={(e) => { if (e.key === 'Enter') open(kind, id); }}
    >
      {children}
    </tr>
  );
}
