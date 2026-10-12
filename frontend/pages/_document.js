import { Head, Html, Main, NextScript } from "next/document";
import prepaint from "../lib/prepaint.json";

// Applied before first paint: the saved language sets lang/dir and the saved
// theme sets data-theme, so a Persian user never sees a left-to-right flash
// and a dark-mode user never sees a white one.
const PREPAINT = prepaint.script;

export default function Document() {
  return (
    <Html lang="en" dir="ltr">
      <Head>
        <meta name="color-scheme" content="light dark" />
        {/* eslint-disable-next-line react/no-danger */}
        <script dangerouslySetInnerHTML={{ __html: PREPAINT }} />
      </Head>
      <body>
        <Main />
        <NextScript />
      </body>
    </Html>
  );
}
