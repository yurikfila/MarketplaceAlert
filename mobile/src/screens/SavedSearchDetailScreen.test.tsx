import { fireEvent } from '@testing-library/react-native';
import { Alert } from 'react-native';

import { ApiError } from '../api/client';
import * as endpoints from '../api/endpoints';
import type { Listing, ListingListResponse, SavedSearch, SavedSearchRunResult } from '../api/types';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { SavedSearchDetailScreen } from './SavedSearchDetailScreen';

jest.mock('../api/endpoints');

const mockedGetSavedSearch = endpoints.getSavedSearch as jest.MockedFunction<typeof endpoints.getSavedSearch>;
const mockedListListings = endpoints.listListings as jest.MockedFunction<typeof endpoints.listListings>;
const mockedRunSavedSearch = endpoints.runSavedSearch as jest.MockedFunction<typeof endpoints.runSavedSearch>;
const mockedUpdateSavedSearch = endpoints.updateSavedSearch as jest.MockedFunction<typeof endpoints.updateSavedSearch>;
const mockedDeleteSavedSearch = endpoints.deleteSavedSearch as jest.MockedFunction<typeof endpoints.deleteSavedSearch>;

const SAMPLE: SavedSearch = {
  id: 7,
  query: 'Makita drill',
  marketplaces: ['ebay', 'etsy'],
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
    saved_search_id: 7,
    ...overrides,
  };
}

function listingsPage(items: Listing[], totalCount = items.length): ListingListResponse {
  return { items, limit: 5, offset: 0, total_count: totalCount };
}

beforeEach(() => {
  mockedGetSavedSearch.mockResolvedValue(SAMPLE);
  mockedListListings.mockResolvedValue(listingsPage([listing()], 1));
});

afterEach(() => {
  jest.clearAllMocks();
});

describe('SavedSearchDetailScreen', () => {
  it('shows the query, brand-cased marketplace chips, and status', async () => {
    // No listings mocked for this one test, so "eBay" only ever appears
    // once (the marketplace chip) - keeps this assertion unambiguous.
    mockedListListings.mockResolvedValue(listingsPage([], 0));
    const { findByText, getByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });

    expect(await findByText('Makita drill')).toBeTruthy();
    expect(getByText('eBay')).toBeTruthy(); // brand-cased, not raw "ebay"
    expect(getByText('Etsy')).toBeTruthy();
    expect(getByText('Active')).toBeTruthy();
  });

  it('shows the total listings-found count from the saved_search_id-filtered listings response', async () => {
    mockedListListings.mockResolvedValue(listingsPage([listing()], 42));

    const { findByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });

    expect(await findByText('42')).toBeTruthy();
    expect(mockedListListings).toHaveBeenCalledWith(
      expect.objectContaining({ saved_search_id: 7, sort: 'newest' }),
    );
  });

  it('shows the latest listings, tappable and consistent with the Listings screen cards', async () => {
    const { findByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });

    expect(await findByText('Latest listings')).toBeTruthy();
    expect(await findByText('Makita 18V Cordless Drill Driver')).toBeTruthy();
    expect(await findByText('USD 120')).toBeTruthy(); // proves the real ListingCard is reused, not a duplicate rendering
  });

  it('shows an empty message when this saved search has found no listings yet', async () => {
    mockedListListings.mockResolvedValue(listingsPage([], 0));

    const { findByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });

    expect(await findByText('No listings discovered by this saved search yet.')).toBeTruthy();
  });

  it('Run Now refreshes both the saved search detail and the latest listings', async () => {
    const runResult: SavedSearchRunResult = {
      saved_search_id: 7,
      query: 'Makita drill',
      marketplaces: { ebay: { new_count: 2, already_seen_count: 0, error: null } },
      total_new_count: 2,
      total_already_seen_count: 0,
    };
    mockedRunSavedSearch.mockResolvedValue(runResult);

    const { findByText, getByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });
    await findByText('Makita drill');
    expect(mockedListListings).toHaveBeenCalledTimes(1);

    await fireEvent.press(getByText('Run now'));

    expect(await findByText('Last run: 2 new, 0 already seen')).toBeTruthy();
    // "eBay" appears in both the marketplace chip and the run-result row
    // (and, since a listing is mocked, the ListingCard badge too) - all
    // brand-cased is exactly the point, so >=1 is what matters here.
    expect(mockedGetSavedSearch).toHaveBeenCalledTimes(2); // initial + post-run refresh
    expect(mockedListListings).toHaveBeenCalledTimes(2); // initial + post-run refresh
  });

  it('shows a full-screen error with retry when the saved search itself fails to load', async () => {
    mockedGetSavedSearch.mockRejectedValueOnce(new ApiError('Could not reach the server.', 'network'));

    const { findByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });

    expect(await findByText('Could not reach the server.')).toBeTruthy();
    expect(await findByText('Try again')).toBeTruthy();
  });

  it('Pause/Resume toggles is_active', async () => {
    mockedUpdateSavedSearch.mockResolvedValue({ ...SAMPLE, is_active: false });

    const { findByText, getByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });
    await findByText('Makita drill');

    await fireEvent.press(getByText('Pause'));

    expect(mockedUpdateSavedSearch).toHaveBeenCalledWith(7, { is_active: false });
  });

  it('Delete requires confirmation and then removes the saved search', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation((_title, _msg, buttons) => {
      const destructive = buttons?.find((b) => b.style === 'destructive');
      destructive?.onPress?.();
    });
    mockedDeleteSavedSearch.mockResolvedValue(undefined);

    const { findByText, getByText } = await renderWithNavigation(SavedSearchDetailScreen, { id: 7 });
    await findByText('Makita drill');

    await fireEvent.press(getByText('Delete'));

    expect(alertSpy).toHaveBeenCalled();
    expect(mockedDeleteSavedSearch).toHaveBeenCalledWith(7);
  });
});
