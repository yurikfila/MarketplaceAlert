import { fireEvent } from '@testing-library/react-native';

import * as endpoints from '../api/endpoints';
import { ApiError } from '../api/client';
import type { MarketplaceInfo } from '../api/types';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { CreateSearchScreen } from './CreateSearchScreen';

jest.mock('../api/endpoints');

const mockedGetMarketplaces = endpoints.getMarketplaces as jest.MockedFunction<typeof endpoints.getMarketplaces>;
const mockedCreateSavedSearch = endpoints.createSavedSearch as jest.MockedFunction<typeof endpoints.createSavedSearch>;

const MARKETPLACES: MarketplaceInfo[] = [
  { id: 'etsy', name: 'Etsy', configured: true, available: true },
  { id: 'ebay', name: 'eBay', configured: true, available: true },
];

describe('CreateSearchScreen', () => {
  beforeEach(() => {
    mockedGetMarketplaces.mockResolvedValue(MARKETPLACES);
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('blocks submission and shows a field error when the query is blank', async () => {
    const { findByText, getByText } = await renderWithNavigation(CreateSearchScreen);

    await findByText('Etsy'); // wait for marketplaces to load
    await fireEvent.press(getByText('Create search'));

    expect(await findByText('Enter a search keyword or phrase.')).toBeTruthy();
    expect(mockedCreateSavedSearch).not.toHaveBeenCalled();
  });

  it('blocks submission when no marketplace is selected', async () => {
    const { findByText, getByText, getByLabelText } = await renderWithNavigation(CreateSearchScreen);

    await findByText('Etsy');
    await fireEvent.changeText(getByLabelText('Search keyword or phrase'), 'Makita drill');
    await fireEvent.press(getByText('Create search'));

    expect(await findByText('Select at least one marketplace.')).toBeTruthy();
    expect(mockedCreateSavedSearch).not.toHaveBeenCalled();
  });

  it('submits a well-formed search with the selected marketplace and default interval', async () => {
    mockedCreateSavedSearch.mockResolvedValue({
      id: 1,
      query: 'Makita drill',
      marketplaces: ['etsy'],
      is_active: true,
      scan_interval_seconds: 60,
      created_at: '2026-08-21T00:00:00Z',
      updated_at: '2026-08-21T00:00:00Z',
      last_scanned_at: null,
    });

    const { findByText, getByText, getByLabelText } = await renderWithNavigation(CreateSearchScreen);

    await findByText('Etsy');
    await fireEvent.changeText(getByLabelText('Search keyword or phrase'), 'Makita drill');
    await fireEvent.press(getByText('Etsy'));
    await fireEvent.press(getByText('Create search'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockedCreateSavedSearch).toHaveBeenCalledWith({
      query: 'Makita drill',
      marketplaces: ['etsy'],
      scan_interval_seconds: 60,
      is_active: true,
    });
  });

  it('shows a server-side error message when submission fails', async () => {
    mockedCreateSavedSearch.mockRejectedValue(new ApiError('scan_interval_seconds must be at least 60', 'http', 422));

    const { findByText, getByText, getByLabelText } = await renderWithNavigation(CreateSearchScreen);

    await findByText('Etsy');
    await fireEvent.changeText(getByLabelText('Search keyword or phrase'), 'Makita drill');
    await fireEvent.press(getByText('Etsy'));
    await fireEvent.press(getByText('Create search'));

    expect(await findByText('scan_interval_seconds must be at least 60')).toBeTruthy();
  });

  it('renders and can submit a new marketplace (Reverb) the moment the API reports it, with no mobile code change', async () => {
    // Proves the marketplace selector is purely driven by
    // GET /api/v1/marketplaces - this test adds "reverb" only to the
    // mocked API response, never to CreateSearchScreen.tsx itself, and
    // it still renders as a selectable chip and reaches the submitted
    // payload.
    mockedGetMarketplaces.mockResolvedValue([
      ...MARKETPLACES,
      { id: 'reverb', name: 'Reverb', configured: true, available: true },
    ]);
    mockedCreateSavedSearch.mockResolvedValue({
      id: 2,
      query: 'Fender Stratocaster',
      marketplaces: ['reverb'],
      is_active: true,
      scan_interval_seconds: 60,
      created_at: '2026-08-22T00:00:00Z',
      updated_at: '2026-08-22T00:00:00Z',
      last_scanned_at: null,
    });

    const { findByText, getByText, getByLabelText } = await renderWithNavigation(CreateSearchScreen);

    expect(await findByText('Reverb')).toBeTruthy();
    await fireEvent.changeText(getByLabelText('Search keyword or phrase'), 'Fender Stratocaster');
    await fireEvent.press(getByText('Reverb'));
    await fireEvent.press(getByText('Create search'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockedCreateSavedSearch).toHaveBeenCalledWith({
      query: 'Fender Stratocaster',
      marketplaces: ['reverb'],
      scan_interval_seconds: 60,
      is_active: true,
    });
  });

  it('renders and can submit a second new marketplace (Bonanza) added only to mocked API data, alongside Reverb', async () => {
    // Same proof as the Reverb test above, for the next connector added -
    // confirms this isn't a one-off coincidence tied to a single
    // marketplace id, but the general "driven entirely by the API"
    // behavior this screen is built on.
    mockedGetMarketplaces.mockResolvedValue([
      ...MARKETPLACES,
      { id: 'reverb', name: 'Reverb', configured: true, available: true },
      { id: 'bonanza', name: 'Bonanza', configured: true, available: true },
    ]);
    mockedCreateSavedSearch.mockResolvedValue({
      id: 3,
      query: 'Fender Stratocaster',
      marketplaces: ['bonanza'],
      is_active: true,
      scan_interval_seconds: 60,
      created_at: '2026-08-22T00:00:00Z',
      updated_at: '2026-08-22T00:00:00Z',
      last_scanned_at: null,
    });

    const { findByText, getByText, getByLabelText } = await renderWithNavigation(CreateSearchScreen);

    expect(await findByText('Bonanza')).toBeTruthy();
    await fireEvent.changeText(getByLabelText('Search keyword or phrase'), 'Fender Stratocaster');
    await fireEvent.press(getByText('Bonanza'));
    await fireEvent.press(getByText('Create search'));

    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(mockedCreateSavedSearch).toHaveBeenCalledWith({
      query: 'Fender Stratocaster',
      marketplaces: ['bonanza'],
      scan_interval_seconds: 60,
      is_active: true,
    });
  });

  it('labels an unconfigured marketplace (e.g. Reverb with no token set) distinctly, still from API data alone', async () => {
    mockedGetMarketplaces.mockResolvedValue([
      ...MARKETPLACES,
      { id: 'reverb', name: 'Reverb', configured: false, available: false },
    ]);

    const { findByText } = await renderWithNavigation(CreateSearchScreen);

    expect(await findByText('Reverb (not configured)')).toBeTruthy();
  });
});
