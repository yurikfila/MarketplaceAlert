import { displayNameForMarketplace } from './marketplaces';

describe('displayNameForMarketplace', () => {
  it.each([
    ['ebay', 'eBay'],
    ['etsy', 'Etsy'],
    ['mock', 'Mock'],
    ['reverb', 'Reverb'],
    ['bonanza', 'Bonanza'],
  ])('maps %s to %s', (id, expected) => {
    expect(displayNameForMarketplace(id)).toBe(expected);
  });

  it('title-cases an unrecognized marketplace id rather than showing it raw', () => {
    expect(displayNameForMarketplace('futuremarket')).toBe('Futuremarket');
  });

  it('title-cases a multi-word unrecognized id', () => {
    expect(displayNameForMarketplace('future_market')).toBe('Future Market');
  });
});
