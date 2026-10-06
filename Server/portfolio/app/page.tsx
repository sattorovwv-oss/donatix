'use client';

import Scene from '@/components/scene/Scene';
import Nav from '@/components/ui/Nav';
import Hero from '@/components/sections/Hero';
import About from '@/components/sections/About';
import Skills from '@/components/sections/Skills';
import Projects from '@/components/sections/Projects';
import Process from '@/components/sections/Process';
import OrderForm from '@/components/sections/OrderForm';
import Contact from '@/components/sections/Contact';

export default function Home() {
  return (
    <>
      <Scene />
      <Nav />

      <div className="page">
        <main>
          {/* Первый экран оставлен «чистым»: город виден в полную силу */}
          <Hero />

          {/* Ниже читается текст, поэтому сцена приглушается вуалью —
              атмосфера сохраняется, но контраст остаётся на стороне контента */}
          <div className="content-veil">
            <About />
            <Skills />
            <Projects />
            <Process />
            <OrderForm />
            <Contact />
          </div>
        </main>
      </div>
    </>
  );
}
