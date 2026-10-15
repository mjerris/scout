import { useColorScheme } from 'react-native';

// The browser page's palette (src/scout/phone.html), so the two look like one product.
const light = {
  bg: '#f6f5f1',
  panel: '#ffffff',
  ink: '#1d1c1a',
  muted: '#6f6c66',
  line: '#e4e1da',
  accent: '#c4673f',
  ok: '#2f7d4f',
  bad: '#b3402f',
};
export type Theme = typeof light;
const dark: Theme = {
  bg: '#1b1a18',
  panel: '#24231f',
  ink: '#ecebe6',
  muted: '#9c988f',
  line: '#37352f',
  accent: '#e08a62',
  ok: '#5cb27e',
  bad: '#e0705c',
};

export function useTheme(): Theme {
  return useColorScheme() === 'dark' ? dark : light;
}
