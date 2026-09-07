import { Nav } from "@/components/Nav";
import { Hero } from "@/components/Hero";
import { Features } from "@/components/Features";
import { Screens } from "@/components/Screens";
import { DownloadCta } from "@/components/DownloadCta";
import { Maker } from "@/components/Maker";
import { Footer } from "@/components/Footer";

export default function Home() {
  return (
    <>
      <Nav />
      <main>
        <Hero />
        <Features />
        <Screens />
        <DownloadCta />
        <Maker />
      </main>
      <Footer />
    </>
  );
}
