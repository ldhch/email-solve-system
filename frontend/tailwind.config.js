/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Mailroom palette: cool paper + steel-blue accent (deliberately not
        // the default Tailwind blue), restrained risk hues.
        ink: "#1B1E23",
        sub: "#6B7280",
        line: "#E4E6EA",
        page: "#F5F6F7",
        accent: "#2E5D86",
        "accent-tint": "#EEF3F8",
        risk: {
          high: "#B4442F",
          "high-tint": "#FBEEE9",
          medium: "#9C731A",
          "medium-tint": "#F7F1DF",
          low: "#2E7D5B",
          "low-tint": "#E9F2ED",
        },
      },
    },
  },
  plugins: [],
};
