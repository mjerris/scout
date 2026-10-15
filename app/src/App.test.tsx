import { render, screen } from '@testing-library/react-native';

import App from './App';

test('renders', () => {
  render(<App />);
  expect(screen.getByText('Scout')).toBeTruthy();
});
