import 'react-native-gesture-handler/jestSetup';

// `@expo/vector-icons` eagerly imports every icon set it bundles, and
// (in the versions currently installed) that chain reaches `expo-font`,
// which imports `expo-asset` without declaring it as its own dependency -
// npm only ever installs `expo-asset` nested under `expo`'s own
// node_modules per the lockfile, so Jest's module resolver (unlike
// Metro's, which never hits this) can't find it from `expo-font`'s
// location. Real icon rendering isn't under test anywhere in this repo -
// this stub keeps that pre-existing dependency gap from blocking any
// test that merely renders an icon.
jest.mock('@expo/vector-icons', () => {
  const React = require('react');
  const { Text } = require('react-native');
  return {
    Ionicons: (props: Record<string, unknown>) => React.createElement(Text, props, props.name),
  };
});
