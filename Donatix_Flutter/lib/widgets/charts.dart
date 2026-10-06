import 'dart:math';
import 'package:flutter/material.dart';
import 'ui.dart';

class CandleChart extends StatefulWidget {
  final List candles;
  final String Function(dynamic) money;
  const CandleChart({super.key, required this.candles, required this.money});
  @override
  State<CandleChart> createState() => _CandleChartState();
}

class _CandleChartState extends State<CandleChart> {
  double count = 60, offset = 0, startCount = 60, startOffset = 0;
  Offset? touch;
  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (c, box) {
      final n = widget.candles.length;
      final visible = count.clamp(min(12, max(1, n)), max(12, n)).toDouble();
      final end = (n - offset.clamp(0, max(0, n - visible))).round().clamp(
        0,
        n,
      );
      final begin = max(0, end - visible.round());
      final slice = widget.candles.sublist(begin, end);
      List? selected;
      if (touch != null && slice.isNotEmpty) {
        selected =
            slice[((touch!.dx / max(1, box.maxWidth - 80)) * slice.length)
                    .floor()
                    .clamp(0, slice.length - 1)]
                as List;
      }
      return Column(
        children: [
          Semantics(
            label:
                'Свечной график D-коина. Переместите пальцем для просмотра истории, двумя пальцами измените масштаб.',
            child: GestureDetector(
              onScaleStart: (_) {
                startCount = count;
                startOffset = offset;
              },
              onScaleUpdate: (d) => setState(() {
                count = (startCount / d.scale).clamp(12, max(12, n)).toDouble();
                offset =
                    (startOffset -
                            d.focalPointDelta.dx /
                                max(1, box.maxWidth - 80) *
                                count)
                        .clamp(0, max(0, n - count))
                        .toDouble();
                startOffset = offset;
              }),
              onLongPressStart: (d) => setState(() => touch = d.localPosition),
              onLongPressMoveUpdate: (d) =>
                  setState(() => touch = d.localPosition),
              onLongPressEnd: (_) => setState(() => touch = null),
              child: SizedBox(
                height: 280,
                width: double.infinity,
                child: CustomPaint(
                  painter: _Candles(
                    slice,
                    Theme.of(context).dividerColor,
                    Theme.of(context).colorScheme.onSurface,
                    touch,
                    widget.money,
                  ),
                ),
              ),
            ),
          ),
          if (selected != null)
            Text(
              '${DateTime.fromMillisecondsSinceEpoch((selected[0] as num).toInt()).toLocal()}\nОткр. ${widget.money(selected[1])} · Макс. ${widget.money(selected[2])}\nМин. ${widget.money(selected[3])} · Закр. ${widget.money(selected[4])}',
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 11),
            ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Text(
                '← листайте → · два пальца — масштаб',
                style: TextStyle(fontSize: 11),
              ),
              TextButton(
                onPressed: () => setState(() {
                  offset = 0;
                  count = 60;
                }),
                child: const Text('Сейчас'),
              ),
            ],
          ),
        ],
      );
    },
  );
}

void _label(
  Canvas canvas,
  String value,
  Offset point,
  Color color, {
  double size = 10,
}) {
  final p = TextPainter(
    text: TextSpan(
      text: value,
      style: TextStyle(fontFamily: 'Onest', fontSize: size, color: color),
    ),
    textDirection: TextDirection.ltr,
  )..layout();
  p.paint(canvas, point);
}

class _Candles extends CustomPainter {
  final List data;
  final Color grid, foreground;
  final Offset? touch;
  final String Function(dynamic) money;
  _Candles(this.data, this.grid, this.foreground, this.touch, this.money);
  @override
  void paint(Canvas canvas, Size size) {
    if (data.isEmpty) return;
    final active = data.where((r) => (r[2] as num) > 0).toList();
    if (active.isEmpty) {
      _label(
        canvas,
        'Цена начнётся с первой выполненной покупки',
        const Offset(8, 130),
        foreground,
      );
      return;
    }
    double lo = active.map((r) => (r[3] as num).toDouble()).reduce(min),
        hi = active.map((r) => (r[2] as num).toDouble()).reduce(max);
    final padding = max((hi - lo) * .12, max(hi.abs() * .002, 1e-12));
    lo = max(0, lo - padding);
    hi += padding;
    final w = max(1.0, size.width - 80), h = size.height - 32;
    double y(num v) => 8 + (hi - v) / max(1e-12, hi - lo) * (h - 8);
    final bw = w / data.length;
    final line = Paint()..strokeWidth = 1;
    for (var i = 0; i <= 4; i++) {
      final v = lo + (hi - lo) * i / 4;
      line.color = grid;
      canvas.drawLine(Offset(0, y(v)), Offset(w, y(v)), line);
      _label(canvas, money(v), Offset(w + 4, y(v) - 6), foreground, size: 9);
    }
    canvas.save();
    canvas.clipRect(Rect.fromLTWH(0, 0, w, h));
    for (var i = 0; i < data.length; i++) {
      final r = data[i] as List, x = (i + .5) * bw;
      if ((r[2] as num) <= 0) continue;
      final o = (r[1] as num).toDouble(), cl = (r[4] as num).toDouble();
      line.color = cl >= o ? const Color(0xff16c784) : const Color(0xffea3943);
      canvas.drawLine(
        Offset(x, y(r[2] as num)),
        Offset(x, y(r[3] as num)),
        line,
      );
      canvas.drawRect(
        Rect.fromLTWH(
          x - bw * .36,
          min(y(o), y(cl)),
          max(2.0, bw * .72),
          max(2.0, (y(o) - y(cl)).abs()),
        ),
        line,
      );
    }
    if (touch != null) {
      line.color = foreground.withValues(alpha: .4);
      canvas.drawLine(Offset(touch!.dx, 0), Offset(touch!.dx, h), line);
    }
    canvas.restore();
    for (final i in {0, data.length ~/ 2, data.length - 1}) {
      final d = DateTime.fromMillisecondsSinceEpoch(
        (data[i][0] as num).toInt(),
      ).toLocal();
      _label(
        canvas,
        '${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}',
        Offset(((i + .5) * bw - 18).clamp(0, max(0, w - 36)), h + 8),
        foreground,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _Candles oldDelegate) => true;
}

class OrdersChart extends StatefulWidget {
  final List series;
  const OrdersChart({super.key, required this.series});
  @override
  State<OrdersChart> createState() => _OrdersChartState();
}

class _OrdersChartState extends State<OrdersChart> {
  int? selected;
  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (c, box) => Column(
      children: [
        GestureDetector(
          onTapDown: (d) {
            if (widget.series.isNotEmpty) {
              setState(
                () => selected =
                    ((d.localPosition.dx / box.maxWidth) * widget.series.length)
                        .floor()
                        .clamp(0, widget.series.length - 1),
              );
            }
          },
          child: SizedBox(
            height: 230,
            width: double.infinity,
            child: CustomPaint(
              painter: _OrderLines(
                widget.series,
                Theme.of(context).dividerColor,
                Theme.of(context).colorScheme.onSurface,
                selected,
              ),
            ),
          ),
        ),
        Wrap(
          spacing: 14,
          children: const [
            Text('● Создано', style: TextStyle(color: accent)),
            Text('● Выполнено', style: TextStyle(color: Colors.green)),
            Text('● Возвраты', style: TextStyle(color: Colors.red)),
          ],
        ),
        if (selected != null && selected! < widget.series.length)
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: Text(
              '${widget.series[selected!]['label']}: создано ${widget.series[selected!]['created']}, выполнено ${widget.series[selected!]['done']}, возвраты ${widget.series[selected!]['refunded']}',
            ),
          ),
      ],
    ),
  );
}

class _OrderLines extends CustomPainter {
  final List data;
  final Color grid, foreground;
  final int? selected;
  _OrderLines(this.data, this.grid, this.foreground, this.selected);
  @override
  void paint(Canvas canvas, Size size) {
    if (data.isEmpty) return;
    final peak = max(
      1,
      data.map((s) => (s['created'] as num).toInt()).reduce(max),
    );
    final top = [
      4,
      8,
      12,
      20,
      40,
      60,
      100,
      200,
      400,
      1000,
      2000,
      4000,
      10000,
    ].firstWhere((v) => v >= peak, orElse: () => (peak ~/ 10000 + 1) * 10000);
    final w = size.width - 32, h = size.height - 32;
    final pen = Paint()
      ..color = grid
      ..strokeWidth = 1;
    for (var i = 0; i <= 4; i++) {
      final y = h - h * i / 4;
      canvas.drawLine(Offset(28, y), Offset(size.width, y), pen);
      _label(
        canvas,
        '${(top * i / 4).round()}',
        Offset(0, max(0, y - 7)),
        foreground,
      );
    }
    for (final e in {
      'created': accent,
      'done': Colors.green,
      'refunded': Colors.red,
    }.entries) {
      final points = List.generate(
        data.length,
        (i) => Offset(
          28 + (data.length == 1 ? w / 2 : i * w / (data.length - 1)),
          h - (data[i][e.key] as num) / top * h,
        ),
      );
      final path = Path()..moveTo(points.first.dx, points.first.dy);
      for (var i = 0; i < points.length - 1; i++) {
        final p0 = points[max(0, i - 1)],
            p1 = points[i],
            p2 = points[i + 1],
            p3 = points[min(points.length - 1, i + 2)];
        path.cubicTo(
          p1.dx + (p2.dx - p0.dx) / 6,
          min(max(p1.dy, p2.dy), p1.dy + (p2.dy - p0.dy) / 6),
          p2.dx - (p3.dx - p1.dx) / 6,
          min(max(p1.dy, p2.dy), p2.dy - (p3.dy - p1.dy) / 6),
          p2.dx,
          p2.dy,
        );
      }
      pen
        ..color = e.value
        ..strokeWidth = 2.4
        ..style = PaintingStyle.stroke;
      if (points.length == 1) {
        canvas.drawCircle(points.first, 3, Paint()..color = e.value);
      } else {
        canvas.drawPath(path, pen);
      }
    }
    for (final i in {0, data.length ~/ 2, data.length - 1}) {
      _label(
        canvas,
        text(data[i]['label']),
        Offset(
          (28 + i * w / max(1, data.length - 1) - 16).clamp(
            0,
            max(0, size.width - 36),
          ),
          h + 10,
        ),
        foreground,
      );
    }
    if (selected != null && selected! < data.length) {
      pen
        ..color = foreground.withValues(alpha: .3)
        ..strokeWidth = 1;
      final x = 28 + selected! * w / max(1, data.length - 1);
      canvas.drawLine(Offset(x, 0), Offset(x, h), pen);
    }
  }

  @override
  bool shouldRepaint(covariant _OrderLines oldDelegate) => true;
}
