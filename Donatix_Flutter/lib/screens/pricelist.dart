import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import '../core/api.dart';
import '../widgets/ui.dart';

class PricelistScreen extends StatefulWidget {
  final DonatixApi api;
  const PricelistScreen({super.key, required this.api});
  @override
  State<PricelistScreen> createState() => _PricelistScreenState();
}

class _PricelistScreenState extends State<PricelistScreen> {
  final captureKey = GlobalKey();
  bool busy = false;
  Future<void> export() async {
    setState(() => busy = true);
    try {
      await WidgetsBinding.instance.endOfFrame;
      final boundary =
          captureKey.currentContext!.findRenderObject()
              as RenderRepaintBoundary;
      final image = await boundary.toImage(pixelRatio: 3);
      final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
      image.dispose();
      if (bytes != null) {
        await const MethodChannel(
          'tj.donatix.app/native',
        ).invokeMethod<void>('shareFile', {
          'bytes': bytes.buffer.asUint8List(),
          'mime': 'image/png',
          'name': 'Donatix-pricelist.png',
        });
      }
    } catch (e) {
      if (mounted) message(context, e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Прайс-лист')),
    body: AsyncPage(
      load: () => widget.api.get('/api/v1/mobile/admin/pricelist'),
      builder: (context, d) => Column(
        children: [
          BusyButton('Скачать / поделиться PNG', busy: busy, onPressed: export),
          const SizedBox(height: 16),
          RepaintBoundary(
            key: captureKey,
            child: Container(
              width: double.infinity,
              padding: const EdgeInsets.all(20),
              decoration: const BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [
                    Color(0xff151242),
                    Color(0xff31216c),
                    Color(0xff101428),
                  ],
                ),
              ),
              child: Theme(
                data: ThemeData.dark().copyWith(
                  textTheme: ThemeData.dark().textTheme.apply(
                    fontFamily: 'Onest',
                    bodyColor: Colors.white,
                    displayColor: Colors.white,
                  ),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      text(d['name']),
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 32,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const SizedBox(height: 6),
                    const Text(
                      'ДОНАТ БЕЗ ОЖИДАНИЯ',
                      style: TextStyle(
                        color: Color(0xffc7d2fe),
                        fontSize: 12,
                        letterSpacing: 2,
                      ),
                    ),
                    const SizedBox(height: 22),
                    for (final s in d['sections'] as List)
                      Container(
                        margin: const EdgeInsets.only(bottom: 16),
                        padding: const EdgeInsets.all(16),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: .08),
                          borderRadius: BorderRadius.circular(16),
                          border: Border.all(
                            color: Colors.white.withValues(alpha: .12),
                          ),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              '${s['game']} · ${s['sub']}',
                              style: const TextStyle(
                                fontSize: 22,
                                fontWeight: FontWeight.w800,
                                color: Colors.white,
                              ),
                            ),
                            const Divider(color: Color(0xff6366f1)),
                            for (final p in s['packs'] as List)
                              Padding(
                                padding: const EdgeInsets.symmetric(
                                  vertical: 5,
                                ),
                                child: Row(
                                  children: [
                                    Expanded(
                                      child: Text(
                                        text(p['short']),
                                        style: const TextStyle(
                                          color: Color(0xffe0e7ff),
                                          fontSize: 13,
                                        ),
                                      ),
                                    ),
                                    Text(
                                      '${p['price']} с.',
                                      style: const TextStyle(
                                        color: Colors.white,
                                        fontWeight: FontWeight.w800,
                                        fontSize: 14,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                          ],
                        ),
                      ),
                    const Text(
                      '① ID игрока   ② Оплата   ③ Донат',
                      style: TextStyle(color: Color(0xffe0e7ff), fontSize: 12),
                    ),
                    const SizedBox(height: 12),
                    Text(
                      '${d['site']}\n${d['support']}',
                      style: const TextStyle(
                        color: Colors.white,
                        fontWeight: FontWeight.w700,
                        fontSize: 13,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    ),
  );
}
