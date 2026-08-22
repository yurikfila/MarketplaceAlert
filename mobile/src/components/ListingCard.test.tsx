import { fireEvent, render } from '@testing-library/react-native';

import type { Listing } from '../api/types';
import { ListingCard } from './ListingCard';
import { openListingUrl } from '../utils/linking';

jest.mock('../utils/linking', () => ({
  openListingUrl: jest.fn(),
}));

const NOW = new Date('2026-08-22T12:00:00.000Z');

function baseListing(overrides: Partial<Listing> = {}): Listing {
  return {
    id: 1,
    marketplace: 'ebay',
    external_listing_id: 'abc123',
    title: 'Makita 18V Cordless Drill Driver',
    price: 120,
    currency: 'USD',
    location: 'Chicago, IL',
    seller: 'tool_outlet',
    condition: 'New',
    listing_url: 'https://ebay.com/itm/abc123',
    image_url: 'https://example.com/drill.jpg',
    source_created_at: null,
    first_discovered_at: '2026-08-22T11:55:00.000Z',
    last_seen_at: '2026-08-22T11:55:00.000Z',
    saved_search_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  jest.useFakeTimers().setSystemTime(NOW);
  jest.clearAllMocks();
});

afterEach(() => {
  jest.useRealTimers();
});

describe('ListingCard', () => {
  it('renders title, marketplace display name, price, condition, location, and seller', async () => {
    const { getByText } = await render(<ListingCard listing={baseListing()} />);

    expect(getByText('Makita 18V Cordless Drill Driver')).toBeTruthy();
    expect(getByText('eBay')).toBeTruthy(); // brand-cased, not raw "ebay"
    expect(getByText('USD 120')).toBeTruthy();
    expect(getByText('New · Chicago, IL')).toBeTruthy();
    expect(getByText('Seller: tool_outlet')).toBeTruthy();
  });

  it('shows a "New" badge (and, distinctly, "Just now" as the relative time) for a listing discovered moments ago', async () => {
    const { getByText } = await render(
      <ListingCard listing={baseListing({ first_discovered_at: NOW.toISOString() })} />,
    );
    expect(getByText('New')).toBeTruthy();
    expect(getByText('Just now')).toBeTruthy();
  });

  it('does not show a "New" badge for a listing discovered well over half an hour ago', async () => {
    const { queryByText } = await render(
      <ListingCard listing={baseListing({ first_discovered_at: '2026-08-22T10:00:00.000Z' })} />,
    );
    expect(queryByText('New')).toBeNull();
  });

  it('omits the price line entirely when price is null - never a placeholder', async () => {
    const { queryByText } = await render(<ListingCard listing={baseListing({ price: null })} />);
    expect(queryByText(/USD/)).toBeNull();
  });

  it('omits the seller line when seller is null', async () => {
    const { queryByText } = await render(<ListingCard listing={baseListing({ seller: null })} />);
    expect(queryByText(/Seller:/)).toBeNull();
  });

  it('shows only condition when location is missing', async () => {
    const { getByText } = await render(<ListingCard listing={baseListing({ condition: 'Used', location: null })} />);
    expect(getByText('Used')).toBeTruthy();
  });

  it('shows only location when condition is missing', async () => {
    const { getByText } = await render(
      <ListingCard listing={baseListing({ condition: null, location: 'Austin, TX' })} />,
    );
    expect(getByText('Austin, TX')).toBeTruthy();
  });

  it('never crashes when every optional field is null', async () => {
    const { getByText } = await render(
      <ListingCard
        listing={baseListing({
          price: null,
          currency: null,
          location: null,
          seller: null,
          condition: null,
          image_url: null,
        })}
      />,
    );
    expect(getByText('Makita 18V Cordless Drill Driver')).toBeTruthy();
  });

  it('renders a very large price with thousands separators', async () => {
    const { getByText } = await render(<ListingCard listing={baseListing({ price: 12500, currency: 'USD' })} />);
    expect(getByText('USD 12,500')).toBeTruthy();
  });

  it('renders a non-USD currency without converting it', async () => {
    const { getByText } = await render(<ListingCard listing={baseListing({ price: 399, currency: 'EUR' })} />);
    expect(getByText('EUR 399')).toBeTruthy();
  });

  it('opens the listing URL via the safe linking helper when pressed', async () => {
    const { getByText } = await render(<ListingCard listing={baseListing()} />);
    await fireEvent.press(getByText('Makita 18V Cordless Drill Driver'));
    expect(openListingUrl).toHaveBeenCalledWith('https://ebay.com/itm/abc123');
  });
});
