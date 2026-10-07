import 'package:flutter/material.dart';

/// Tokens and easing copied from donatix.css; shared by native page layouts.
class SiteColors {
  final bool dark;
  SiteColors(BuildContext c) : dark = Theme.of(c).brightness == Brightness.dark;
  Color get background => Color(dark ? 0xff0e1016 : 0xfff5f6fa);
  Color get surface => Color(dark ? 0xff161922 : 0xffffffff);
  Color get surface2 => Color(dark ? 0xff1d212c : 0xfff1f2f7);
  Color get ink => Color(dark ? 0xffeceef3 : 0xff121521);
  Color get muted => Color(dark ? 0xff9ba2b1 : 0xff5e6577);
  Color get line => Color(dark ? 0xff262b37 : 0xffe7e9f0);
  Color get accent => Color(dark ? 0xff8b8cf8 : 0xff4f46e5);
  Color get accentSoft => Color(dark ? 0xff1e2040 : 0xffeef0ff);
  Color semantic(String kind) => switch (kind) {
    'ok' ||
    'good' ||
    'plus' ||
    'c-done' => Color(dark ? 0xff4ade80 : 0xff157347),
    'bad' ||
    'error' ||
    'danger' ||
    'minus' ||
    'c-ref' => Color(dark ? 0xfff87171 : 0xffc42b2b),
    'warn' => Color(dark ? 0xfff5b942 : 0xff975a00),
    'g2' => Color(dark ? 0xff5cb3f0 : 0xff2481cc),
    'g5' => Color(dark ? 0xfffb923c : 0xffc2410c),
    _ => accent,
  };
  Color soft(String kind) => switch (kind) {
    'ok' || 'good' || 'plus' => Color(dark ? 0xff12261b : 0xffe8f6ee),
    'bad' ||
    'error' ||
    'danger' ||
    'minus' => Color(dark ? 0xff2b1616 : 0xfffdeeee),
    'warn' => Color(dark ? 0xff2a2110 : 0xfffdf5e5),
    'g2' => Color(dark ? 0xff13253a : 0xffe7f2fb),
    'g5' => Color(dark ? 0xff2c1a0e : 0xfffdf1e9),
    _ => accentSoft,
  };
}

const siteEase = Cubic(.16, 1, .3, 1);

/// The site's .985 press transform with keyboard and accessibility activation.
class SitePress extends StatefulWidget {
  final Widget child;
  final VoidCallback onTap;
  const SitePress({super.key, required this.child, required this.onTap});
  @override
  State<SitePress> createState() => _SitePressState();
}

class _SitePressState extends State<SitePress> {
  bool down = false;
  @override
  Widget build(BuildContext context) => AnimatedScale(
    scale: down ? .985 : 1,
    duration: MediaQuery.disableAnimationsOf(context)
        ? Duration.zero
        : const Duration(milliseconds: 180),
    curve: siteEase,
    child: Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: widget.onTap,
        onHighlightChanged: (value) => setState(() => down = value),
        child: widget.child,
      ),
    ),
  );
}

class SiteReveal extends StatefulWidget {
  final Widget child;
  final int index;
  const SiteReveal({super.key, required this.child, this.index = 0});
  @override
  State<SiteReveal> createState() => _SiteRevealState();
}

class _SiteRevealState extends State<SiteReveal>
    with SingleTickerProviderStateMixin {
  late final controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 500),
  );
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (MediaQuery.disableAnimationsOf(context)) {
      controller.value = 1;
    } else {
      controller.forward();
    }
  }

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final start = (widget.index.clamp(0, 4) * .08).toDouble();
    final curve = CurvedAnimation(
      parent: controller,
      curve: Interval(start, 1, curve: siteEase),
    );
    return AnimatedBuilder(
      animation: curve,
      child: widget.child,
      builder: (c, child) => Opacity(
        opacity: curve.value,
        child: Transform.translate(
          offset: Offset(0, 10 * (1 - curve.value)),
          child: child,
        ),
      ),
    );
  }
}

class SiteGrid extends StatelessWidget {
  final List<Widget> children;
  final double minimum, gap, breakpoint;
  final int? columns;
  const SiteGrid({
    super.key,
    required this.children,
    this.minimum = 210,
    this.gap = 14,
    this.columns,
    this.breakpoint = 700,
  });
  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (c, b) {
      final n =
          (columns == null
                  ? ((b.maxWidth + gap) / (minimum + gap)).floor()
                  : b.maxWidth > breakpoint
                  ? columns!
                  : 1)
              .clamp(1, 8);
      final width = (b.maxWidth - gap * (n - 1)) / n;
      return Wrap(
        spacing: gap,
        runSpacing: gap,
        children: [
          for (var i = 0; i < children.length; i++)
            SizedBox(
              width: width,
              child: SiteReveal(index: i, child: children[i]),
            ),
        ],
      );
    },
  );
}

class SiteCard extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  const SiteCard({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(16),
  });
  @override
  Widget build(BuildContext context) {
    final p = SiteColors(context);
    return Container(
      padding: padding,
      decoration: BoxDecoration(
        color: p.surface,
        border: Border.all(color: p.line),
        borderRadius: BorderRadius.circular(16),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: p.dark ? .12 : .04),
            blurRadius: 8,
            offset: const Offset(0, 2),
          ),
        ],
      ),
      child: child,
    );
  }
}
