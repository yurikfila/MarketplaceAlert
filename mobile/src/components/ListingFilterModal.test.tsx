import type { ComponentProps } from 'react';
import { fireEvent, render } from '@testing-library/react-native';

import { activeFilterCount, emptyListingFilters, ListingFilterModal, type ListingFilterValues } from './ListingFilterModal';

/** A node from `render(...).toJSON()`'s tree - either a rendered host
 * element or a plain text/number leaf. */
type JsonNode = { type: string; props: Record<string, unknown>; children: JsonNode[] | null } | string | number;

/** Depth-first search over a `toJSON()` tree for every host node whose
 * type matches `type`. `react-native-safe-area-context` renders
 * `SafeAreaProvider`/`SafeAreaView` down to real host components named
 * exactly `"RNCSafeAreaProvider"`/`"RNCSafeAreaView"` (confirmed by
 * inspecting real rendered output, not assumed) - these are the only
 * public, stable signal this project's testing-library version exposes
 * for "is this instance actually a SafeAreaProvider/SafeAreaView", since
 * `UNSAFE_getByType`-style component-reference queries aren't available
 * here (checked directly against this project's installed
 * @testing-library/react-native and test-renderer type definitions). */
function findAllByHostType(node: JsonNode | null, type: string): Array<{ type: string; props: Record<string, unknown> }> {
  if (node === null || typeof node === 'string' || typeof node === 'number') return [];
  const matches = node.type === type ? [{ type: node.type, props: node.props }] : [];
  const childMatches = (node.children ?? []).flatMap((child) => findAllByHostType(child, type));
  return [...matches, ...childMatches];
}

const MARKETPLACES = [
  { id: 'ebay', name: 'eBay' },
  { id: 'etsy', name: 'Etsy' },
];

const SAVED_SEARCHES = [
  { id: 1, query: 'Makita drill' },
  { id: 2, query: 'Fender Stratocaster' },
];

async function renderModal(overrides: Partial<ComponentProps<typeof ListingFilterModal>> = {}) {
  const onChange = jest.fn();
  const onApply = jest.fn();
  const onClear = jest.fn();
  const onClose = jest.fn();
  const utils = await render(
    <ListingFilterModal
      visible
      onClose={onClose}
      availableMarketplaces={MARKETPLACES}
      availableSavedSearches={SAVED_SEARCHES}
      value={emptyListingFilters()}
      onChange={onChange}
      onApply={onApply}
      onClear={onClear}
      {...overrides}
    />,
  );
  return { ...utils, onChange, onApply, onClear, onClose };
}

describe('ListingFilterModal', () => {
  it('is not rendered in the tree when visible=false', async () => {
    const { queryByText } = await render(
      <ListingFilterModal
        visible={false}
        onClose={jest.fn()}
        availableMarketplaces={MARKETPLACES}
        availableSavedSearches={SAVED_SEARCHES}
        value={emptyListingFilters()}
        onChange={jest.fn()}
        onApply={jest.fn()}
        onClear={jest.fn()}
      />,
    );
    expect(queryByText('Filters & sort')).toBeNull();
  });

  it('shows every marketplace, sort option, and saved search when visible', async () => {
    const { getByText } = await renderModal();
    expect(getByText('eBay')).toBeTruthy();
    expect(getByText('Etsy')).toBeTruthy();
    expect(getByText('Newest')).toBeTruthy();
    expect(getByText('Price: low to high')).toBeTruthy();
    expect(getByText('Makita drill')).toBeTruthy();
    expect(getByText('Fender Stratocaster')).toBeTruthy();
  });

  it('hides the saved-search section entirely when there are no saved searches', async () => {
    const { queryByText } = await renderModal({ availableSavedSearches: [] });
    expect(queryByText('Any saved search')).toBeNull();
  });

  describe('safe-area structure (Android status bar / navigation bar overlap fix)', () => {
    /** Structural assertions, not visible-text ones - appropriate here
     * since the structure itself (a nested `SafeAreaProvider` so insets
     * are measured for the Modal's own Android window, and
     * `SafeAreaView` on the correct edges) is exactly what was broken
     * and exactly what this fix changes. See `ListingFilterModal.tsx`'s
     * own comment above its `SafeAreaProvider` for the full reasoning. */

    it('wraps its content in its own SafeAreaProvider, not just relying on one from an ancestor', async () => {
      const { toJSON } = await renderModal();
      expect(findAllByHostType(toJSON(), 'RNCSafeAreaProvider')).toHaveLength(1);
    });

    it('applies a top-edge SafeAreaView (header) and a bottom-edge SafeAreaView (footer)', async () => {
      const { toJSON } = await renderModal();
      const safeAreaViews = findAllByHostType(toJSON(), 'RNCSafeAreaView');
      const edgesUsed = safeAreaViews.map((el) => el.props.edges);
      // The library normalizes `edges={['top']}`/`['bottom']` into a
      // per-side "off"/"additive" map on the actual rendered host node -
      // confirmed by inspecting real rendered output, not assumed.
      expect(edgesUsed).toContainEqual({ top: 'additive', right: 'off', bottom: 'off', left: 'off' });
      expect(edgesUsed).toContainEqual({ top: 'off', right: 'off', bottom: 'additive', left: 'off' });
    });
  });

  it('toggling a marketplace chip adds it to the draft', async () => {
    const { getByText, onChange } = await renderModal();
    await fireEvent.press(getByText('eBay'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ marketplaces: ['ebay'] }));
  });

  it('toggling an already-selected marketplace removes it from the draft', async () => {
    const { getByText, onChange } = await renderModal({
      value: { ...emptyListingFilters(), marketplaces: ['ebay', 'etsy'] },
    });
    await fireEvent.press(getByText('eBay'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ marketplaces: ['etsy'] }));
  });

  it('selecting a sort option updates the draft', async () => {
    const { getByText, onChange } = await renderModal();
    await fireEvent.press(getByText('Price: high to low'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ sort: 'price_desc' }));
  });

  it('selecting a saved search updates the draft', async () => {
    const { getByText, onChange } = await renderModal();
    await fireEvent.press(getByText('Makita drill'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ savedSearchId: 1 }));
  });

  it('typing a min/max price updates the draft', async () => {
    const { getByLabelText, onChange } = await renderModal();
    await fireEvent.changeText(getByLabelText('Minimum price'), '10');
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ minPrice: '10' }));
  });

  it('calls onApply when "Apply filters" is pressed', async () => {
    const { getByText, onApply } = await renderModal();
    await fireEvent.press(getByText('Apply filters'));
    expect(onApply).toHaveBeenCalled();
  });

  it('calls onClear when "Clear all" is pressed', async () => {
    const { getByText, onClear } = await renderModal();
    await fireEvent.press(getByText('Clear all'));
    expect(onClear).toHaveBeenCalled();
  });

  it('calls onClose when "Done" is pressed', async () => {
    const { getByText, onClose } = await renderModal();
    await fireEvent.press(getByText('Done'));
    expect(onClose).toHaveBeenCalled();
  });
});

describe('activeFilterCount', () => {
  it('is 0 for an empty filter set', () => {
    expect(activeFilterCount(emptyListingFilters())).toBe(0);
  });

  it('counts marketplaces, saved search, min price, max price, and condition independently', () => {
    const value: ListingFilterValues = {
      marketplaces: ['ebay'],
      savedSearchId: 1,
      minPrice: '10',
      maxPrice: '100',
      condition: 'New',
      sort: 'newest',
    };
    expect(activeFilterCount(value)).toBe(5);
  });

  it('does not count sort as an active filter', () => {
    const value: ListingFilterValues = { ...emptyListingFilters(), sort: 'price_asc' };
    expect(activeFilterCount(value)).toBe(0);
  });

  it('does not count whitespace-only price/condition text as active', () => {
    const value: ListingFilterValues = { ...emptyListingFilters(), minPrice: '   ', condition: '  ' };
    expect(activeFilterCount(value)).toBe(0);
  });
});
