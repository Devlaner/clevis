export const metadata = {
  title: "clevis",
  description: "Basic analytics for GitHub repos and organizations",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "Arial, sans-serif", margin: 0, padding: 24 }}>
        <h1>clevis</h1>
        {children}
      </body>
    </html>
  );
}
