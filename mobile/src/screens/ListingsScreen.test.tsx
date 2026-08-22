import { act, fireEvent, within } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import * as endpoints from '../api/endpoints';
import type { Listing, ListingListResponse, MarketplaceInfo, SavedSearch } from '../api/types';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { ListingsScreen } from './ListingsScreen';

jest.mock('../api/endpoints');

const mockedListListings = endpoints.listListings as jest.MockedFunction<typeof endpoints.listListings>;
const mockedGetMarketplaces = endpoints.getMarketplaces as jest.MockedFunction<typeof endpoints.getMarketplaces>;
const mockedListSavedSearches = endpoints.listSavedSearches as jest.MockedFunction<typeof endpoints.listSavedSearches>;

const MARKETPLACES: MarketplaceInfo[] = [
  { id: 'ebay', name: 'eBay', configured: true, available: true },
  { id: 'etsy', name: 'Etsy', configured: true, available: true },
];

const SAVED_SEARCH: SavedSearch = {
  id: 7,
  query: 'Makita drill',
  marketplaces: ['ebay'],
  is_active: true,
  scan_interval_seconds: 300,
  created_at: '2026-08-16T18:10:36Z',
  updated_at: '2026-08-16T18:10:36Z',
  last_scanned_at: '2026-08-22T10:00:00Z',
};

function listing(overrides: Partial<Listing> = {}): Listing {
  return {
    id: 1,
    marketplace: 'ebay',
    external_listing_id: 'abc',
    title: 'Makita 18V Cordless Drill Driver',
    price: 120,
    currency: 'USD',
    location: 'Chicago, IL',
    seller: 'tool_outlet',
    condition: 'New',
    listing_url: 'https://ebay.com/itm/abc',
    image_url: null,
    source_created_at: null,
    first_discovered_at: '2026-08-22T09:00:00Z',
    last_seen_at: '2026-08-22T09:00:00Z',
    saved_search_id: null,
    ...overrides,
  };
}

function page(items: Listing[], totalCount = items.length): ListingListResponse {
  return { items, limit: 20, offset: 0, total_count: totalCount };
}

beforeEach(() => {
  mockedGetMarketplaces.mockResolvedValue(MARKETPLACES);
  mockedListSavedSearches.mockResolvedValue([SAVED_SEARCH]);
});

afterEach(() => {
  jest.clearAllMocks();
});

describe('ListingsScreen', () => {
  it('shows listings and the result count once loaded', async () => {
    mockedListListings.mockResolvedValue(page([listing()], 1));

    const { findByText } = await renderWithNavigation(ListingsScreen);

    expect(await findByText('Makita 18V Cordless Drill Driver')).toBeTruthy();
    expect(await findByText('1 listing')).toBeTruthy();
  });

  it('shows an unfiltered empty state when there are no listings at all', async () => {
    mockedListListings.mockResolvedValue(page([], 0));

    const { findByText } = await renderWithNavigation(ListingsScreen);

    expect(await findByText('No listings yet')).toBeTruthy();
  });

  it('shows a full-screen error with retry when the initial load fails', async () => {
    mockedListListings.mockRejectedValueOnce(new ApiError('Could not reach the server.', 'network'));

    const { findByText } = await renderWithNavigation(ListingsScreen);

    expect(await findByText('Could not reach the server.')).toBeTruthy();
    expect(await findByText('Try again')).toBeTruthy();
  });

  it('opens the filter modal from the Filters button', async () => {
    mockedListListings.mockResolvedValue(page([listing()], 1));

    const { findByText, getByText, getByLabelText } = await renderWithNavigation(ListingsScreen);
    await findByText('Makita 18V Cordless Drill Driver');

    await fireEvent.press(getByLabelText('Filters'));

    expect(getByText('Filters & sort')).toBeTruthy();
  });

  it('applying a marketplace filter refetches with the selected marketplace and shows an active-filter badge', async () => {
    mockedListListings.mockResolvedValue(page([listing()], 1));

    const { findByText, getByText, getByLabelText, getByTestId } = await renderWithNavigation(ListingsScreen);
    await findByText('Makita 18V Cordless Drill Driver');

    await fireEvent.press(getByLabelText('Filters'));
    await fireEvent.press(within(getByTestId('listing-filter-modal')).getByText('eBay'));
    await fireEvent.press(getByText('Apply filters'));

    expect(await findByText('1')).toBeTruthy(); // the badge
    const lastCall = mockedListListings.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ marketplaces: ['ebay'] });
  });

  it('applying a sort mode refetches with that sort and updates the visible sort label', async () => {
    mockedListListings.mockResolvedValue(page([listing()], 1));

    const { findByText, getByText, getByLabelText } = await renderWithNavigation(ListingsScreen);
    await findByText('Makita 18V Cordless Drill Driver');

    await fireEvent.press(getByLabelText('Filters'));
    await fireEvent.press(getByText('Price: low to high'));
    await fireEvent.press(getByText('Apply filters'));

    expect(await findByText('Sort: Price: low to high')).toBeTruthy();
    const lastCall = mockedListListings.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ sort: 'price_asc' });
  });

  it('clearing filters resets the badge and refetches with no filters applied', async () => {
    mockedListListings.mockResolvedValue(page([listing()], 1));

    const { findByText, findByLabelText, getByText, getByLabelText, queryByLabelText, getByTestId } =
      await renderWithNavigation(ListingsScreen);
    await findByText('Makita 18V Cordless Drill Driver');

    await fireEvent.press(getByLabelText('Filters'));
    await fireEvent.press(within(getByTestId('listing-filter-modal')).getByText('eBay'));
    await fireEvent.press(getByText('Apply filters'));
    expect(await findByLabelText('Filters, 1 active')).toBeTruthy();

    await fireEvent.press(getByLabelText('Filters, 1 active'));
    await fireEvent.press(getByText('Clear all'));

    expect(queryByLabelText('Filters, 1 active')).toBeNull();
    const lastCall = mockedListListings.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ marketplaces: undefined });
  });

  it('pull-to-refresh preserves the currently applied filters', async () => {
    mockedListListings.mockResolvedValue(page([listing()], 1));

    const { findByText, getByText, getByLabelText, getByTestId } = await renderWithNavigation(ListingsScreen);
    await findByText('Makita 18V Cordless Drill Driver');

    await fireEvent.press(getByLabelText('Filters'));
    await fireEvent.press(within(getByTestId('listing-filter-modal')).getByText('eBay'));
    await fireEvent.press(getByText('Apply filters'));
    mockedListListings.mockClear();
    mockedListListings.mockResolvedValue(page([listing()], 1));

    // Invoke the RefreshControl's onRefresh directly, rather than relying
    // on native pull-gesture event simulation (no precedent for that
    // elsewhere in this test suite, and it's the RefreshControl's own
    // callback prop - not a DOM-style event - that actually matters here).
    await act(async () => {
      getByTestId('listings-list').props.refreshControl.props.onRefresh();
    });

    expect(mockedListListings).toHaveBeenCalledWith(expect.objectContaining({ marketplaces: ['ebay'] }));
  });

  it('a background auto-refresh failure keeps existing listings visible rather than clearing them', async () => {
    jest.useFakeTimers();
    mockedListListings.mockResolvedValueOnce(page([listing()], 1));

    const { findByText, queryByText, unmount } = await renderWithNavigation(ListingsScreen);
    try {
      expect(await findByText('Makita 18V Cordless Drill Driver')).toBeTruthy();

      mockedListListings.mockRejectedValueOnce(new ApiError('Server unavailable', 'network'));
      await act(async () => {
        jest.advanceTimersByTime(20_000); // DEFAULT_AUTO_REFRESH_INTERVAL_MS
        await Promise.resolve();
      });

      // The listing is still shown, and no error screen replaced it.
      expect(await findByText('Makita 18V Cordless Drill Driver')).toBeTruthy();
      expect(queryByText('Server unavailable')).toBeNull();
    } finally {
      await unmount();
      jest.useRealTimers();
    }
  });

  it('shows a filtered-specific empty message when filters exclude everything', async () => {
    mockedListListings.mockResolvedValueOnce(page([listing()], 1));

    const { findByText, getByText, getByLabelText, getByTestId } = await renderWithNavigation(ListingsScreen);
    await findByText('Makita 18V Cordless Drill Driver');

    mockedListListings.mockResolvedValue(page([], 0));
    await fireEvent.press(getByLabelText('Filters'));
    await fireEvent.press(within(getByTestId('listing-filter-modal')).getByText('eBay'));
    await fireEvent.press(getByText('Apply filters'));

    expect(await findByText('No listings match these filters')).toBeTruthy();
  });

  it('Load more requests the next page and appends results', async () => {
    const first = listing({ id: 1, external_listing_id: 'a', title: 'First item' });
    const second = listing({ id: 2, external_listing_id: 'b', title: 'Second item' });
    mockedListListings.mockResolvedValueOnce(page([first], 2));
    mockedListListings.mockResolvedValueOnce(page([second], 2));

    const { findByText, getByText } = await renderWithNavigation(ListingsScreen);
    await findByText('First item');

    await fireEvent.press(getByText('Load more'));

    expect(await findByText('Second item')).toBeTruthy();
    expect(await findByText('First item')).toBeTruthy();
    const secondCallParams = mockedListListings.mock.calls[1][0];
    expect(secondCallParams).toMatchObject({ offset: 1 });
  });
});
