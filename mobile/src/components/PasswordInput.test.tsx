import { fireEvent, render } from '@testing-library/react-native';

import { PasswordInput } from './PasswordInput';

describe('PasswordInput', () => {
  it('hides the password by default', async () => {
    const { getByLabelText } = await render(
      <PasswordInput value="secret" onChangeText={jest.fn()} accessibilityLabel="Password" />,
    );

    expect(getByLabelText('Password').props.secureTextEntry).toBe(true);
    expect(getByLabelText('Show password')).toBeTruthy();
  });

  it('reveals the password as plain text when the toggle is pressed, and hides it again on a second press', async () => {
    const { getByLabelText, queryByLabelText } = await render(
      <PasswordInput value="secret" onChangeText={jest.fn()} accessibilityLabel="Password" />,
    );

    await fireEvent.press(getByLabelText('Show password'));
    expect(getByLabelText('Password').props.secureTextEntry).toBe(false);
    expect(getByLabelText('Hide password')).toBeTruthy();
    expect(queryByLabelText('Show password')).toBeNull();

    await fireEvent.press(getByLabelText('Hide password'));
    expect(getByLabelText('Password').props.secureTextEntry).toBe(true);
    expect(getByLabelText('Show password')).toBeTruthy();
  });

  it('never changes the password value when toggling visibility', async () => {
    const onChangeText = jest.fn();
    const { getByLabelText } = await render(
      <PasswordInput value="unchanged-value" onChangeText={onChangeText} accessibilityLabel="Password" />,
    );

    await fireEvent.press(getByLabelText('Show password'));
    await fireEvent.press(getByLabelText('Hide password'));

    expect(getByLabelText('Password').props.value).toBe('unchanged-value');
    expect(onChangeText).not.toHaveBeenCalled();
  });

  it('does not submit the form when the toggle is pressed', async () => {
    const onSubmitEditing = jest.fn();
    const { getByLabelText } = await render(
      <PasswordInput
        value="secret"
        onChangeText={jest.fn()}
        accessibilityLabel="Password"
        onSubmitEditing={onSubmitEditing}
      />,
    );

    await fireEvent.press(getByLabelText('Show password'));

    expect(onSubmitEditing).not.toHaveBeenCalled();
  });

  it('exposes an accessible button role for the toggle', async () => {
    const { getByLabelText } = await render(
      <PasswordInput value="secret" onChangeText={jest.fn()} accessibilityLabel="Password" />,
    );

    expect(getByLabelText('Show password').props.accessibilityRole).toBe('button');
  });
});
