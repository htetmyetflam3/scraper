import * as cheerio from "cheerio";

const pages = [
  "https://slrd.gov.mm/laws-regulations/laws",
  "https://slrd.gov.mm/laws-regulations/regulations",
  "https://slrd.gov.mm/laws-regulations/orders",
];

async function getPdfLinks(url) {
  const res = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
  if (!res.ok) throw new Error(`Failed ${url}: ${res.status}`);
  const html = await res.text();
  const $ = cheerio.load(html);

  const links = [];
  $("a[href]").each((_, a) => {
    const href = $(a).attr("href");
    if (!href) return;
    const abs = new URL(href, url).toString();
    if (abs.toLowerCase().endsWith(".pdf")) links.push(abs);
  });
  return links;
}

const all = [];
for (const p of pages) all.push(...await getPdfLinks(p));

const unique = [...new Set(all)];
console.log("Unique PDF links:", unique.length);
unique.forEach(u => console.log(u));