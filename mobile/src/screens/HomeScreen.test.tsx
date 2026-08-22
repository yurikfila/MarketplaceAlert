import * as endpoints from '../api/endpoints';
import type { Listing, ListingListResponse, MobileStatus, SavedSearch } from '../api/types';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { HomeScreen } from './HomeScreen';

jest.mock('../api/endpoints');

const mockedGetStatus = endpoints.getStatus as jest.MockedFunction<typeof endpoints.getStatus>;
const mockedListSavedSearches = endpoints.listSavedSearches as jest.MockedFunction<typeof endpoints.listSavedSearches>;
const mockedListListings = endpoints.listListings as jest.MockedFunction<typeof endpoints.listListings>;

const STATUS: MobileStatus = {
  status: 'ok',
  backend: true,
  database: true,
  telegram_configured: true,
  supported_marketplaces: ['ebay', 'etsy', 'mock'],
};

function savedSearch(overrides: Partial<SavedSearch> = {}): SavedSearch {
  return {
    id: 1,
    query: 'Makita drill',
    marketplaces: ['ebay'],
    is_active: true,
    scan_interval_seconds: 300,
    created_at: '2026-08-16T18:10:36Z',
    updated_at: '2026-08-16T18:10:36Z',
    last_scanned_at: null,
    ...overrides,
  };
}

function listingsPage(items: Listing[] = []): ListingListResponse {
  return { items, limit: 5, offset: 0, total_count: items.length };
}

beforeEach(() => {
  mockedGetStatus.mockResolvedValue(STATUS);
  mockedListSavedSearches.mockResolvedValue([]);
  mockedListListings.mockResolvedValue(listingsPage());
});

afterEach(() => {
  jest.clearAllMocks();
});

describe('HomeScreen', () => {
  it('shows marketplace names with brand casing, not raw ids', async () => {
    const { findByText, queryByText } = await renderWithNavigation(HomeScreen);

    expect(await findByText(/eBay/)).toBeTruthy();
    expect(queryByText(/Ebay/)).toBeNull();
  });

  it('shows "None yet" for last scan activity when no saved search has ever run', async () => {
    mockedListSavedSearches.mockResolvedValue([savedSearch({ last_scanned_at: null })]);

    const { findByText } = await renderWithNavigation(HomeScreen);

    expect(await findByText('Last scan activity: None yet')).toBeTruthy();
  });

  it('shows the most recent last_scanned_at across every saved search, as relative time', async () => {
    mockedListSavedSearches.mockResolvedValue([
      savedSearch({ id: 1, last_scanned_at: '2026-08-22T10:00:00.000Z' }),
      savedSearch({ id: 2, last_scanned_at: '2026-08-22T11:45:00.000Z' }), // most recent
      savedSearch({ id: 3, last_scanned_at: '2026-08-22T09:00:00.000Z' }),
    ]);
    jest.useFakeTimers().setSystemTime(new Date('2026-08-22T12:00:00.000Z'));

    const { findByText } = await renderWithNavigation(HomeScreen);

    expect(await findByText('Last scan activity: 15 min ago')).toBeTruthy();
    jest.useRealTimers();
  });

  it('shows the active saved-search count', async () => {
    mockedListSavedSearches.mockResolvedValue([
      savedSearch({ id: 1, is_active: true }),
      savedSearch({ id: 2, is_active: false }),
      savedSearch({ id: 3, is_active: true }),
    ]);

    const { findByText } = await renderWithNavigation(HomeScreen);

    expect(await findByText('2 active')).toBeTruthy();
  });

  it('shows recently discovered listings using the same ListingCard as the Listings screen', async () => {
    mockedListListings.mockResolvedValue(
      listingsPage([
        {
          id: 1,
          marketplace: 'ebay',
          external_listing_id: 'abc',
          title: 'Makita 18V Cordless Drill Driver',
          price: 120,
          currency: 'USD',
          location: null,
          seller: null,
          condition: null,
          listing_url: 'https://ebay.com/itm/abc',
          image_url: null,
          source_created_at: null,
          first_discovered_at: '2026-08-22T09:00:00Z',
          last_seen_at: '2026-08-22T09:00:00Z',
          saved_search_id: null,
        },
      ]),
    );

    const { findByText } = await renderWithNavigation(HomeScreen);

    expect(await findByText('Makita 18V Cordless Drill Driver')).toBeTruthy();
    expect(await findByText('USD 120')).toBeTruthy();
  });
});
