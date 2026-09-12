import { fireEvent } from '@testing-library/react-native';

import { ApiError } from '../api/client';
import * as endpoints from '../api/endpoints';
import type { AdminUserListResponse } from '../api/types';
import { renderWithNavigation } from '../testUtils/renderWithNavigation';
import { AdminUsersScreen } from './AdminUsersScreen';

jest.mock('../api/endpoints');

const mockedListAdminUsers = endpoints.listAdminUsers as jest.MockedFunction<typeof endpoints.listAdminUsers>;

const SAMPLE_RESPONSE: AdminUserListResponse = {
  total_users: 2,
  users: [
    {
      id: 1,
      email: 'admin@example.com',
      created_at: '2026-01-01T00:00:00Z',
      is_admin: true,
      is_active: true,
      saved_search_count: 0,
    },
    {
      id: 2,
      email: 'shopper@example.com',
      created_at: '2026-02-15T00:00:00Z',
      is_admin: false,
      is_active: true,
      saved_search_count: 3,
    },
  ],
};

describe('AdminUsersScreen', () => {
  afterEach(() => {
    jest.clearAllMocks();
  });

  it('shows a loading state before the request resolves', async () => {
    mockedListAdminUsers.mockReturnValue(new Promise(() => {})); // never resolves
    const { getByLabelText } = await renderWithNavigation(AdminUsersScreen);

    expect(getByLabelText('Loading users…')).toBeTruthy();
  });

  it('shows the total user count once loaded', async () => {
    mockedListAdminUsers.mockResolvedValue(SAMPLE_RESPONSE);
    const { findByText } = await renderWithNavigation(AdminUsersScreen);

    expect(await findByText('Users: 2')).toBeTruthy();
  });

  it('renders each users email, admin/user label, and saved search count', async () => {
    mockedListAdminUsers.mockResolvedValue(SAMPLE_RESPONSE);
    const { findByText, getByText } = await renderWithNavigation(AdminUsersScreen);

    expect(await findByText('admin@example.com')).toBeTruthy();
    expect(getByText('shopper@example.com')).toBeTruthy();
    expect(getByText('Admin')).toBeTruthy();
    expect(getByText('User')).toBeTruthy();
    expect(getByText('Saved searches: 3')).toBeTruthy();
    expect(getByText('Saved searches: 0')).toBeTruthy();
  });

  it('shows an empty state when there are no registered users', async () => {
    mockedListAdminUsers.mockResolvedValue({ total_users: 0, users: [] });
    const { findByText } = await renderWithNavigation(AdminUsersScreen);

    expect(await findByText('No registered users')).toBeTruthy();
  });

  it('shows an error state with retry when the request fails', async () => {
    mockedListAdminUsers.mockRejectedValue(new ApiError('The server reported an error (HTTP 403).', 'http', 403));
    const { findByText } = await renderWithNavigation(AdminUsersScreen);

    expect(await findByText('The server reported an error (HTTP 403).')).toBeTruthy();
    expect(await findByText('Try again')).toBeTruthy();
  });

  it('retries the request when "Try again" is pressed', async () => {
    mockedListAdminUsers.mockRejectedValueOnce(new ApiError('Network error', 'network'));
    mockedListAdminUsers.mockResolvedValueOnce(SAMPLE_RESPONSE);
    const { findByText, getByText } = await renderWithNavigation(AdminUsersScreen);

    await findByText('Network error');
    await fireEvent.press(getByText('Try again'));

    expect(await findByText('Users: 2')).toBeTruthy();
    expect(mockedListAdminUsers).toHaveBeenCalledTimes(2);
  });

  it('calls listAdminUsers - never a hand-rolled fetch or a client-side email check', async () => {
    mockedListAdminUsers.mockResolvedValue(SAMPLE_RESPONSE);
    await renderWithNavigation(AdminUsersScreen);

    expect(mockedListAdminUsers).toHaveBeenCalledTimes(1);
    expect(mockedListAdminUsers).toHaveBeenCalledWith();
  });
});
